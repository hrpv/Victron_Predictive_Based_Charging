#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# pylint: disable=logging-fstring-interpolation,protected-access
# pylint: disable=too-many-locals,too-many-branches,too-many-statements
# pylint: disable=too-many-instance-attributes,attribute-defined-outside-init
# pylint: disable=too-many-return-statements,too-many-arguments,too-many-positional-arguments
# pylint: disable=no-else-return,too-few-public-methods,broad-exception-caught
# pylint: disable=missing-module-docstring,too-many-lines,wrong-import-position
# pylint: disable=unused-argument,line-too-long,pointless-string-statement
# PEP 563: alle Typ-Annotationen werden lazy ausgewertet (Strings).
# Verhindert NameError bei TYPE_CHECKING-only Imports (VictronModbus, EvccMonitor)
# zur Laufzeit auf Python 3.10+.
from __future__ import annotations

"""
controller.py — Ladesteuerungs-Engine für Solar Batterie Manager
================================================================
Ausgelagert aus battery_manager.py ab v3.0.10.5.

Enthält:
  - EnergyAccumulator  : Trapez-Integration PV/Last/Batterie zu Tages-Wh
  - PowerSmoother      : Gleitender Durchschnitt PV/Last (Rauschunterdrückung)
  - ChargeController   : Kernlogik decide(), run_cycle(), build_schedule(),
                         _simulate_hour(), _update_history()

Importiert von: battery_manager.py (Instanziierung in main())
"""

import json
import logging
import math
import os
import tempfile
import time
from collections import deque
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from models import SystemState, HourlyForecast, HourlyHistory
    from modbus_victron import VictronModbus
    from evcc import EvccMonitor
    from forecast import ForecastManager

# Zur Laufzeit gebraucht (nicht nur TYPE_CHECKING)
from models import SystemState, HourlyForecast, HourlyHistory  # pylint: disable=ungrouped-imports
from forecast import ForecastManager  # pylint: disable=ungrouped-imports


class EnergyAccumulator:
    """Integriert Leistungsmesswerte trapezfoermig zu Tages-Energiewerten."""

    # Mindestabstand zwischen zwei Integrationsschritten.
    # Verhindert verrauschte Energiestatistiken bei sehr schnellen
    # aufeinanderfolgenden Aufrufen (z.B. Modbus-Retry-Bursts).
    MIN_UPDATE_INTERVAL_S: float = 1.0

    def __init__(self):
        self._last_ts: Optional[float] = None
        self._last_pv:  float = 0.0
        self._last_ld:  float = 0.0
        self._last_bat: float = 0.0
        self._day: Optional[date] = None
        self.pv_kwh:   float = 0.0
        self.load_kwh: float = 0.0
        self.bat_wh:   float = 0.0   # signed Wh: + = Laden, - = Entladen

    def update(self, pv_w: float, load_w: float, bat_w: float = 0.0):
        """Integriert Leistungsmesswerte trapezförmig; setzt Zähler bei Tageswechsel zurück."""
        now_ts = time.monotonic()
        today  = date.today()
        if self._day and self._day != today:   # Tageswechsel
            self.pv_kwh = self.load_kwh = self.bat_wh = 0.0
        self._day = today
        if self._last_ts is not None:
            dt_s = now_ts - self._last_ts
            # Zu schnelle Updates ueberspringen – Messwert wird gemerkt,
            # Zeitstempel aber NICHT aktualisiert, damit das naechste
            # gueltiges Update das korrekte Intervall sieht.
            if dt_s < self.MIN_UPDATE_INTERVAL_S:
                self._last_pv  = pv_w
                self._last_ld  = load_w
                self._last_bat = bat_w
                return
            dt_h = dt_s / 3600.0
            self.pv_kwh   += self._last_pv  * dt_h / 1000.0
            self.load_kwh += self._last_ld  * dt_h / 1000.0
            self.bat_wh   += self._last_bat * dt_h   # Wh, signed (W * h = Wh)
        self._last_ts  = now_ts
        self._last_pv  = pv_w
        self._last_ld  = load_w
        self._last_bat = bat_w


# ─────────────────────────────────────────────
# Ladeentscheidungs-Engine
# ─────────────────────────────────────────────

class PowerSmoother:
    """
    Gleitender Durchschnitt fuer PV- und Last-Leistung.
    Glaettet kurzzeitige Schwankungen (Wolken, Lastspitzen)
    ueber N Zyklen, ohne die Reaktionsfaehigkeit zu verlieren.
    """

    def __init__(self, window_cycles: int = 3):
        self._window = window_cycles
        self._pv_samples: list[float] = []
        self._load_samples: list[float] = []

    def update(self, pv_w: float, load_w: float) -> tuple[float, float]:
        """
        Fuegt neue Werte hinzu und gibt geglaettete Werte zurueck.
        """
        self._pv_samples.append(pv_w)
        self._load_samples.append(load_w)
        # Nur die letzten N Werte behalten
        if len(self._pv_samples) > self._window:
            self._pv_samples = self._pv_samples[-self._window:]
        if len(self._load_samples) > self._window:
            self._load_samples = self._load_samples[-self._window:]

        pv_smooth = sum(self._pv_samples) / len(self._pv_samples)
        load_smooth = sum(self._load_samples) / len(self._load_samples)
        return pv_smooth, load_smooth

    def reset(self):
        """Puffer leeren (z.B. nach langer Pause)."""
        self._pv_samples.clear()
        self._load_samples.clear()


class ChargeController:
    """
    Kernlogik: entscheidet Modus und Ladestrom.

    Prioritaeten (absteigend):
      1. evcc Schnellladen        -> Steuerung abgeben (nichts schreiben)
      2. Notfall SOC              -> Volllast sofort
      3. Vollladung faellig       -> auf max_soc laden (Balancing)
      4. Nacht                    -> kein Laden
      5. Morgen-Verzoegerung      -> warte auf PV wenn Prognose ausreicht
      6. PV-Ueberschuss           -> laden mit max_a (oder reduced_a im Optimal-Fenster)
      7. Trickle                  -> sanft laden wenn SOC weit unter Ziel
      8. Ziel erreicht            -> Stop
    """

    def __init__(self, cfg: dict, state: SystemState,
                 forecast: ForecastManager, victron: VictronModbus,
                 evcc: EvccMonitor, logger: logging.Logger):
        self.cfg      = cfg
        self.state    = state
        self.forecast = forecast
        self.victron  = victron
        self.evcc     = evcc
        self.logger   = logger
        self.bat      = cfg["battery"]
        self.cc       = cfg["charging"]
        self._ramp_current: float = 0.0
        self._state_file = Path(cfg["dashboard"].get("state_file", "state.json"))
        self._load_persistent()
        # Energiebasis nach Neustart: persistiert in state.json
        # damit der EnergyAccumulator nach Neustart korrekt weiterlaeuft
        self._energy_base_pv:   float = 0.0
        self._energy_base_load: float = 0.0
        self._energy_base_bat:  float = 0.0   # Wh, signed
        self._load_energy_base()
        # Hysterese: Mindestdauer einer Ladeentscheidung
        self._last_decision_ts: float = 0.0
        self._last_decision_result: tuple[float, str, str] = (0.0, "idle", "Initialisierung")
        self._min_charge_duration_s: float = self.cc.get("min_charge_duration_minutes", 10) * 60
        # Letzter geschriebener Wert (nach Rampe) fuer Schreib-Hysterese
        self._last_written_ramped_a: float = 0.0
        # Persistenter Zustand: nur alle 5 Minuten auf SD-Karte schreiben
        self._last_persistent_save: float = 0.0
        # v3.0.9.28: Hysterese auf dem QUANTISIERTEN Sollwert, nicht dem
        # gerampeten Wert. Verhindert Oszillation 10A↔15A durch Ramp-Artefakte
        # wenn decide() zwischen Stufen wechselt und _ramp() schrittweise
        # durchläuft.
        self._last_quantized_target_a: float = 0.0
        # v3.0.0: Morgen-Notladung Tracking
        self._morning_emergency_done: bool = False
        # v3.0.0: Auto-Reset Vollladung Tracking
        # v3.0.13: Kriterium verschaerft - Trigger erst bei SOC >= 98% UND
        # battery_voltage >= full_charge_min_voltage (Default 55V). Reiner
        # SOC-Wert ist bei LiFePO4 in der flachen Spannungskurve kein
        # verlaessliches Vollladungs-Signal (siehe CHANGELOG 2026-06-28:
        # Auto-Reset erfolgte bei SOC 98% trotz nicht erreichter Zellspannung,
        # Balancing brach vorzeitig ab, Folgenacht-Vollladung blieb wirkungslos).
        self._soc_98_reached_at: Optional[datetime] = None
        # v3.0.1: Cellbalancing-Haltezeit: SOC >= max_soc fuer min. N Stunden halten
        self._balancing_hold_until: float = 0.0
        # v3.0.13.3: Sperre gegen mehrfaches Hold-Neustarten am selben Tag.
        # dyn_target bleibt oft den ganzen Tag >= 98.0 (z.B. weil der reguläre
        # Nachtverbrauchspfad das verlangt, nicht nur das 10-Tage-Zellbalancing-
        # Intervall), waehrend battery_voltage nachmittags um die 55V-Schwelle
        # pendelt (PV-/Lastschwankung). Ohne Sperre wuerde jedes erneute kurze
        # Ueberschreiten von U>=55V einen kompletten neuen 119-Minuten-Hold
        # auslösen, obwohl bereits ein vollstaendiger Zyklus gelaufen ist und
        # days_since_full_charge laengst auf 0 zurueckgesetzt wurde (Symptom:
        # wiederholte TRICKLE-Neustarts am Nachmittag, siehe CHANGELOG 2026-06-28).
        # Wird bei vollstaendig durchlaufenem Hold (Ende der Haltezeit erreicht,
        # NICHT bei vorzeitigem Abbruch) gesetzt und beim Mitternachts-Reset
        # zusammen mit _balancing_reset_date wieder freigegeben.
        self._balancing_completed_today: bool = False
        # v3.0.9.3: Leistungsglättung (3 Zyklen gleitender Durchschnitt)
        smooth_window = self.cc.get("power_smooth_window_cycles", 3)
        self._power_smoother = PowerSmoother(window_cycles=smooth_window)
        # v3.0.9.26: required_a-Glaettung (gleitender Mittelwert)
        smooth_window_req = self.cc.get("required_a_smooth_window", 3)
        self._required_a_history: deque = deque(maxlen=smooth_window_req)
        # Tagesreset-Tracking: Mitternachts-Reset der Balancing-Timer
        self._balancing_reset_date: str = date.today().isoformat()

        # v3.0.11: Optimal-Fenster Prognose-basierte Stundensteuerung
        # Kein Filter, keine Quantisierung, keine Glättung im Optimal-Fenster.
        # Strom wird stündlich aus dem Prognose-Netto-Überschuss gesetzt.
        # Defizit aus Vorjahr-Stunde wird auf Reststunden aufgeteilt.
        #
        # _opt_plan_hour:    Stunde für die der aktuelle Setpoint gilt (-1 = noch kein Plan)
        # _opt_planned_wh:   Geplante Ladeenergie dieser Stunde [Wh] (positiv = Laden)
        # _opt_bat_wh_snapshot: bat_wh_total beim Stundenbeginn (für Ist-Energie-Messung)
        # _opt_carried_wh:   Kumuliertes Defizit aus Vorjahr-Stunden [Wh]
        # _opt_setpoint_a:   Aktuell gültiger Ladestrom aus Stundenplanung [A]
        self._opt_plan_hour: int = -1
        self._opt_planned_wh: float = 0.0
        self._opt_bat_wh_snapshot: float = 0.0
        self._opt_carried_wh: float = 0.0
        self._opt_setpoint_a: float = 0.0

        # Winterpause: Flag ob der einmalige Modbus-Write fuer den aktuellen
        # Winterpause-Zeitraum bereits erfolgt ist. Reset beim Verlassen des
        # Zeitraums, damit im naechsten Winter wieder einmalig geschrieben wird.
        self._winter_pause_write_done: bool = False
        # Tracking-Felder die bedingt gesetzt werden (hier initialisiert um W0201 zu vermeiden)
        self._min_soc_force_ts: float = 0.0
        self._last_evcc_active: bool = False

    def _load_energy_base(self):
        """Laedt letzten bekannten Energie-Akkumulatorstand aus state.json."""
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text(encoding="utf-8"))
                saved_date = data.get("energy_date", "")
                today = date.today().isoformat()
                if saved_date == today:
                    # Selber Tag: Basis wiederherstellen
                    self._energy_base_pv   = float(data.get("energy_base_pv",   0.0))
                    self._energy_base_load = float(data.get("energy_base_load", 0.0))
                    self._energy_base_bat  = float(data.get("energy_base_bat",  0.0))
                    self.logger.info(
                        f"Energie-Basis geladen: PV={self._energy_base_pv:.3f} kWh, "
                        f"Last={self._energy_base_load:.3f} kWh")
                else:
                    # Neuer Tag: Basis auf 0 (Tageswechsel)
                    self._energy_base_pv   = 0.0
                    self._energy_base_load = 0.0
        except Exception as e:
            self.logger.warning(f"Energie-Basis konnte nicht geladen werden: {e}")

    def _load_persistent(self):
        if not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            last = data.get("last_full_charge_date", "")
            self.state.last_full_charge_date = last
            if last:
                d = date.fromisoformat(last)
                self.state.days_since_full_charge = (date.today() - d).days
                self.logger.info(
                    f"Letzte Vollladung: {last} "
                    f"({self.state.days_since_full_charge} Tage)")
        except Exception as e:
            self.logger.warning(f"Persistenter Zustand nicht lesbar: {e}")

    def _save_persistent(self):
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            # Aktuellen Gesamtenergie-Stand = Akkumulator + Basis aus letztem Neustart
            pv_total   = self.state.pv_energy_today_kwh   + self._energy_base_pv
            load_total = self.state.load_energy_today_kwh + self._energy_base_load
            bat_total  = self.state.bat_energy_today_wh   + self._energy_base_bat
            payload = json.dumps({
                "last_full_charge_date":   self.state.last_full_charge_date,
                "days_since_full_charge":  self.state.days_since_full_charge,
                "soc":                     self.state.soc,
                "charge_mode":             self.state.charge_mode,
                "charge_current_setpoint": self.state.charge_current_setpoint,
                "timestamp":               datetime.now().isoformat(),
                # Energiebasis fuer Neustart-Wiederherstellung
                "energy_date":             date.today().isoformat(),
                "energy_base_pv":          round(pv_total,   3),
                "energy_base_load":        round(load_total, 3),
                "energy_base_bat":         round(bat_total,  1),
            }, indent=2)
            # Atomic write: erst in Temp-Datei, dann atomares rename().
            # Verhindert korrupte state.json bei Stromausfall waehrend des Schreibens.
            fd, tmp_path = tempfile.mkstemp(
                dir=self._state_file.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())   # sicherstellen, dass Daten auf SD sind
                os.replace(tmp_path, self._state_file)  # atomares rename
            except Exception:
                os.unlink(tmp_path)        # Temp aufraemen bei Fehler
                raise
            self._last_persistent_save = time.monotonic()
        except Exception as e:
            self.logger.error(f"Zustand nicht speicherbar: {e}")

    # --- Hilfsfunktionen ---

    def _needs_full_charge(self) -> bool:
        return self.state.days_since_full_charge >= self.bat.get(
            "full_charge_interval_days", 10)

    def _in_winter_pause(self) -> bool:
        """Prueft ob heutiges Datum im konfigurierten Winterpause-Zeitraum liegt.

        winter_pause_start/-end sind MM-DD-Strings, jahresunabhaengig.
        Liegt start > end (z.B. 11-01 .. 02-28), wird der Jahreswechsel
        ueberbrueckt.
        """
        if not self.cc.get("winter_pause_enabled", False):
            return False
        start_m, start_d = (int(x) for x in self.cc.get("winter_pause_start", "11-01").split("-"))
        end_m, end_d = (int(x) for x in self.cc.get("winter_pause_end", "02-28").split("-"))
        start, end = (start_m, start_d), (end_m, end_d)
        today_md = (date.today().month, date.today().day)
        if start <= end:
            return start <= today_md <= end
        return today_md >= start or today_md <= end

    def _is_morning_window(self) -> bool:
        h = datetime.now().hour
        # v3.0.9: Dynamisch ab Sonnenaufgang (GPS+Datum)
        sunrise, _, _ = self.forecast._calculate_sun_times(date.today())
        morn_s = int(sunrise)
        morn_e = morn_s + self.cc.get("morning_delay_h", 4)
        return morn_s <= h < morn_e

    def _is_night(self) -> bool:
        h = datetime.now().hour
        # Astronomische Zeiten aus GPS + Datum (v3.0.9)
        sunrise, sunset, _ = self.forecast._calculate_sun_times(date.today())
        night_start = max(12, min(23, math.ceil(sunset)))
        night_end   = max(0, min(11, math.floor(sunrise)))
        return (h >= night_start or h < night_end)

    def _soc_for_kwh(self, kwh: float) -> float:
        return (kwh / self.bat["capacity_kwh"]) * 100.0

    def _calculate_target_soc(self) -> float:
        """Berechnet dynamisches target_soc basierend auf Nachtverbrauch.

        target_soc = max(min_soc, emergency_charge_soc) + (night_consumption_kWh / capacity_kWh) * 100%
        Wenn > 98% -> 98%. Wenn days_since_full_charge > 10 -> 98% (Vollladung).
        """
        min_required = max(self.bat["min_soc"], self.cc.get("emergency_charge_soc", 25))
        night_cons = self.forecast.night_consumption_kwh()
        self._update_night_consumption_display()
        capacity = self.bat["capacity_kwh"]

        if self.state.days_since_full_charge >= self.bat.get("full_charge_interval_days", 10):
            return 98.0

        target = min_required + (night_cons / capacity) * 100.0
        target = min(target, 98.0)
        return max(target, min_required)

    def _update_night_consumption_display(self) -> None:
        """Aktualisiert state.forecast_consumption_night_kwh fuer Dashboard-Anzeige.
        Wird einmal pro Zyklus in decide() aufgerufen."""
        night_cons = self.forecast.night_consumption_kwh()
        self.state.forecast_consumption_night_kwh = round(night_cons, 2)

    def _get_optimal_charge_window(self) -> tuple[int, int]:
        """Gibt (start, end) des optimalen Lade-Fensters um Sonnenhoechststand zurueck.

        Sonnenhoechststand wird dynamisch aus GPS + Datum berechnet
        (v3.0.9: _calculate_sun_times). Offset bleibt konfigurierbar.
        """
        offset = self.cc.get("solar_noon_offset_hours", 2)
        # Dynamischer Sonnenhoechststand (lokale Zeit, float)
        _, _, solar_noon = self.forecast._calculate_sun_times(date.today())
        noon = int(round(solar_noon))
        return (noon - offset, noon + offset)

    # --- Hauptentscheidung ---

    def decide(self) -> tuple[float, str, str]:
        """Gibt (Ladestrom_A, Modus, Begruendung) zurueck.
           Ladestrom -1 bedeutet: nicht schreiben (evcc hat Kontrolle)."""

        soc       = self.state.soc
        max_soc   = self.bat["max_soc"]
        hyst      = self.cc.get("soc_hysteresis", 2)
        emergency = self.cc.get("emergency_charge_soc", 25)
        max_a     = self.bat["max_charge_current"]
        trickle_a = self.bat.get("trickle_current", 5)
        min_required = max(self.bat["min_soc"], emergency)

        # ── Dynamisches target_soc (v3.0.0) ───────────────────
        dyn_target = self._calculate_target_soc()
        self.state.target_soc = round(dyn_target, 1)

        # ── Zentrale Leistungsglättung (v3.0.9.11) ────────────
        # Einmal pro Zyklus geglättet, alle Verzweigungen nutzen dieselben Werte
        pv_raw   = self.state.pv_power_w
        load_raw = self.state.load_power_w
        pv_w, load_w = self._power_smoother.update(pv_raw, load_raw)

        # NEU: Zuverlässiger Überschuss über Grid-Leistung (v3.0.9.11)
        # Grid-Power: + = Bezug (Netzimport), - = Einspeisung (Export)
        # Export = wirklicher Überschuss, der ins Netz geht oder in den Akku könnte.
        # Grid ist zuverlässiger als PV-Load weil es alle Verluste, DC-Lasten
        # und den aktuellen Batterieladestrom automatisch mitberücksichtigt.
        grid_w = self.state.grid_power_w

        # Surplus = negativer Grid-Bezug = Export = verfügbarer Überschuss
        # Mindestens 50W Export (Toleranz für Messrauschen), sonst kein echter Überschuss.
        # Bei positiver Grid-Leistung (Import): surplus = 0 (kein Überschuss).
        surplus_from_grid = max(0.0, -grid_w) if grid_w < -50 else 0.0

        # Rohüberschuss: PV - Last (ohne Batteriekorrektur).
        # load_w ist AC-seitig gemessen und enthält den Batterie-Ladestrom NICHT ->
        # ein zusätzlicher Abzug von battery_power_w wäre eine Doppelkorrektur.
        # raw_surplus_w dient sowohl als Fallback (wenn Grid-Messung nahe 0) als
        # auch als Guard im Trickle-Pfad. (v3.0.9.20 Bugfix: war pv_w - load_w - battery_charge_w)
        raw_surplus_w = max(0.0, pv_w - load_w)

        # Verwende Grid-basierten Überschuss wenn verfügbar (aussagekräftig),
        # sonst Rohüberschuss PV-Last. Grid ist zuverlässiger weil es alle Verluste
        # und DC-Lasten automatisch mitberücksichtigt.
        if abs(grid_w) > 50:  # Grid-Messung aktiv und aussagekräftig
            surplus_w = surplus_from_grid
            surplus_source = "Grid"
        else:
            surplus_w = raw_surplus_w
            surplus_source = "PV-Load"

        self.logger.debug(
            f"Surplus: Grid={grid_w:.0f}W, PV={pv_w:.0f}W, Load={load_w:.0f}W, "
            f"raw={raw_surplus_w:.0f}W -> surplus={surplus_w:.0f}W "
            f"(Quelle: {surplus_source})"
        )

        # ── Effektiver Min-SOC: evcc kann diesen angehoben haben ─
        effective_min_soc = self.bat["min_soc"]
        if self.state.evcc_discharge_locked:
            effective_min_soc = max(effective_min_soc,
                                    self.evcc.evcc_min_soc)
            self.logger.debug(
                f"evcc MinSoc-Sperre aktiv: effektiver Min-SOC = "
                f"{effective_min_soc:.0f}% (Reg 2901={self.evcc.evcc_min_soc:.0f}%)")

        # ── 0. Winterpause: Regelung komplett aussetzen ───────
        # Hoechste Prioritaet, explizit auch vor ESS-Notfallzustaenden -
        # Nutzer-Vorgabe: ausserhalb des Winterpause-Zeitraums voll automatisch,
        # waehrend der Winterpause kein Management mehr. Beim Eintritt wird
        # einmalig max_a geschrieben, danach nur noch -1 (kein Write).
        if self._in_winter_pause():
            if not self._winter_pause_write_done:
                self.victron.set_max_charge_current(max_a)
                self._winter_pause_write_done = True
                self.logger.info(
                    f"Winterpause: einmaliger Write {max_a:.0f}A, Regelung pausiert")
            return -1, "winter_pause", (
                f"Winterpause aktiv ({self.cc.get('winter_pause_start')} - "
                f"{self.cc.get('winter_pause_end')}), keine Regelung")
        else:
            self._winter_pause_write_done = False

        # ── 1. ESS State 11/12: Entladen gesperrt oder Netzladung erzwungen ──
        # HOECHSTE PRIORITAET: Hardware-Eingriff durch Victron ESS.
        # State 11: SOC < MinSOC -> Entladen blockiert, Last aus Netz
        # State 12: SOC >5% unter MinSOC -> Zwangsladung aus Netz
        # In beiden Faellen: sofort max. Ladestrom, unabhaengig von Tag/Nacht/Morgen-Logik.
        # Muss VOR Morgen-Notladung geprueft werden, da sonst ab morning_delay_start_hour
        # der Morgen-Block greift und ESS State 11 ignoriert wird (v3.0.7 Bugfix).
        if self.state.ess_battery_life_state in {11, 12}:
            return max_a, "charging", (
                f"ESS State {self.state.ess_battery_life_state}: "
                f"Notladung/Entladesperre -> max {max_a}A")

        # ── Morgen-Notladung (v3.0.9.7) ────────────────────────
        # SOC unter Minimum: sofort mit max_a laden bis min_soc erreicht.
        # Keine Proportionallogik, kein PV-Ueberschuss-Check.
        if self._is_morning_window() and soc < min_required:
            self._morning_emergency_done = False
            return max_a, "charging", (
                f"Morgen-Notladung: SOC {soc:.1f}% < {min_required}% "
                f"-> {max_a:.0f}A bis min_soc")

        # Morgen-Notladung abgeschlossen markieren
        if soc >= min_required:
            self._morning_emergency_done = True

        # ── 2. Notfall ────────────────────────────────────────
        if soc <= emergency:
            return max_a, "charging", (
                f"NOTFALL: SOC {soc:.1f}% <= {emergency}% -> sofort laden")

        # ── 3. Vollladung faellig (Zellbalancing) ─────────────
        # Wird durch dyn_target >= 98.0 abgedeckt (v3.0.0)
        # v3.0.13.1: Luecke geschlossen. Vorher endete dieser Block bereits bei
        # soc >= max_soc - hyst (typ. 96%), waehrend der Trickle/Hold-Block
        # unten erst ab soc >= dyn_target (98%) UND (seit v3.0.13.0) zusaetzlich
        # battery_voltage >= full_charge_min_voltage greift. Im Bereich
        # 96-98% SOC (oder bei 98% SOC ohne ausreichende Spannung) griff somit
        # WEDER dieser Block NOCH der Trickle-Block - die Steuerung fiel durch
        # in Optimal-Fenster/Idle-Logik, die den Strom abrupt auf wenige A
        # (z.B. min_charge_current) reduzierte, obwohl die Vollladung noch
        # nicht abgeschlossen war (Symptom: 50A -> 3A Sprung trotz "Vollladung
        # faellig"). Fix: dieser Block bleibt jetzt aktiv, bis SOC UND Spannung
        # gemeinsam die Vollladungs-Schwelle erreichen - exakt die Bedingung,
        # die den Trickle-Block weiter unten freischaltet. Dadurch ist die
        # Uebergabe 50A -> Trickle nahtlos, ohne Luecke.
        full_min_v = self.bat.get("full_charge_min_voltage", 55.0)
        full_charge_complete = soc >= 98.0 and self.state.battery_voltage >= full_min_v
        if dyn_target >= 98.0 and not full_charge_complete:
            return max_a, "full_charge", (
                f"Vollladung faellig ({self.state.days_since_full_charge} Tage) "
                f"-> lade auf {max_soc}% (aktuell {soc:.1f}%, "
                f"U={self.state.battery_voltage:.1f}V, Ziel U>={full_min_v:.1f}V)")

        # ── 4. Nacht -> kein Laden ────────────────────────────
        if self._is_night():
            return 0, "idle", f"Nacht: kein Laden (SOC {soc:.1f}%)"

        # Ziel bereits erreicht?
        # v3.0.9.10: Asymmetrische Hysterese – Abschalten erst bei soc >= dyn_target
        # (nicht bei dyn_target - hyst). Nachladen weiter unten bei soc < dyn_target - hyst.
        # Verhindert systematischen 2%-Unterschuss durch symmetrische Hysterese.
        if soc >= dyn_target:
            # v3.0.1: Cellbalancing-Haltezeit: bei max_soc mindestens N Stunden trickle halten
            # v3.0.13: Haltezeit bricht sofort ab, wenn SOC oder Spannung unter die
            # Hysterese-Schwelle fallen. Ein zwischenzeitlicher Abfall bedeutet,
            # dass die Vollladung NICHT stabil erreicht war -> kein gueltiger
            # Balancing-Zyklus. _balancing_hold_until wird auf 0 gesetzt, damit
            # run_cycle() bei erneutem Erreichen von SOC>=98% & Spannung>=Trigger
            # einen frischen Hold mit voller Dauer startet (siehe run_cycle()).
            # full_min_v bereits oben (Block 3) berechnet.
            full_v_hyst = self.bat.get("full_charge_voltage_hysteresis", 0.1)
            hold_active = time.monotonic() < self._balancing_hold_until
            if hold_active:
                # v3.0.13: Waehrend der Haltezeit muss SOC >= 98% bleiben (nicht nur
                # >= max_soc - hyst) - das war die urspruengliche Trigger-Schwelle und
                # soll auch fuer das Halten gelten. Spannung mit 0.1V-Hysterese.
                soc_ok = soc >= 98.0
                v_ok = self.state.battery_voltage >= (full_min_v - full_v_hyst)
                if soc_ok and v_ok:
                    remaining_min = int((self._balancing_hold_until - time.monotonic()) / 60)
                    # v3.0.13.2 (Pending-Idee aus Notizen): Keine unnoetige
                    # Stromreduktion beim Eintritt in die Trickle-Phase. Kommt der
                    # Hold direkt aus Block 3 (Vollladung faellig, max_a/50A), ist
                    # eine Reduktion auf trickle_a (20A) technisch nicht begruendet -
                    # SOC/Spannung sind bereits erreicht, der hoehere Strom richtet
                    # keinen Schaden an und erzwingt nur einen unnoetigen Modbus-Write
                    # (50A -> 20A) ohne Vorteil fuers Cellbalancing. _ramp_current
                    # spiegelt den aktuell tatsaechlich aktiven (gerampten) Strom des
                    # Vorzyklus wider, noch bevor _ramp() fuer DIESEN Zyklus laeuft.
                    hold_current_a = max(trickle_a, self._ramp_current)
                    return hold_current_a, "trickle", (
                        f"Cellbalancing: SOC {soc:.1f}% >= 98%, "
                        f"U={self.state.battery_voltage:.1f}V >= {full_min_v - full_v_hyst:.1f}V, "
                        f"halte {hold_current_a:.0f} A noch {remaining_min} min")
                else:
                    # Abbruchgrund fuer Log/Diagnose
                    abort_reason = []
                    if not soc_ok:
                        abort_reason.append(f"SOC {soc:.1f}% < 98%")
                    if not v_ok:
                        abort_reason.append(
                            f"U {self.state.battery_voltage:.1f}V < {full_min_v - full_v_hyst:.1f}V")
                    self.logger.info(
                        f"Cellbalancing abgebrochen ({', '.join(abort_reason)}) "
                        f"-> Vollladung bleibt faellig")
                    self._balancing_hold_until = 0.0
                    self._soc_98_reached_at = None
            elif self._balancing_hold_until > 0.0:
                # v3.0.13.3: hold_active ist False, aber _balancing_hold_until war
                # gesetzt (>0) -> der Countdown ist NATUERLICH abgelaufen (nicht durch
                # vorzeitigen Abbruch oben, der _balancing_hold_until bereits auf 0.0
                # gesetzt haette). Das markiert einen erfolgreich abgeschlossenen
                # Cellbalancing-Zyklus fuer heute. _balancing_completed_today sperrt
                # run_cycle() davor, spaeter am selben Tag bei einem kurzen erneuten
                # Ueberschreiten von battery_voltage>=full_charge_min_voltage (z.B.
                # PV-Schwankung) einen weiteren vollen 119-Minuten-Hold zu starten
                # (Symptom vor diesem Fix: wiederholte TRICKLE-Neustarts am
                # Nachmittag trotz laengst erfolgtem Auto-Reset, CHANGELOG 2026-06-28).
                self._balancing_completed_today = True
                self._balancing_hold_until = 0.0
            return 0, "idle", (
                f"Ziel {dyn_target:.0f}% erreicht (SOC {soc:.1f}%)")

        # ── 5. Morgen-Fenster mit adaptiver Logik (v3.0.2) ────
        # WICHTIG: Das Morgen-Verzoegerungs-Fenster muss bis zum Optimal-Fenster
        # reichen, sonst entsteht eine Luecke (z.B. morn_e=10, opt_start=11)
        # in der sofort geladen wird  -  obwohl das Optimal-Fenster noch ausreichend
        # PV verspricht.
        h_now = datetime.now().hour
        opt_start, opt_end = self._get_optimal_charge_window()
        # v3.0.9: Morgenfenster dynamisch ab Sonnenaufgang + morning_delay_h
        sunrise, _, _ = self.forecast._calculate_sun_times(date.today())
        morn_s = int(sunrise)
        morn_e = morn_s + self.cc.get("morning_delay_h", 4)
        effective_morn_e = max(morn_e, opt_start)

        if morn_s <= h_now < effective_morn_e:
            needed_kwh = max(0.0, (dyn_target - soc) / 100.0 * self.bat["capacity_kwh"])
            fc_list = self.forecast.get_forecast()
            # v3.0.9.27: Netto-Ueberschuss statt Brutto-PV  -  needed_kwh ist die
            # Energie die der Akku benoetigt, der Vergleichswert muss ebenfalls Netto sein.
            net_in_optimal = sum(max(0.0, f.net_kwh) for f in fc_list if opt_start <= f.hour <= opt_end)

            # Warte auf Optimal-Fenster wenn:
            # - genug Netto-Ueberschuss dort erwartet wird  UND
            # - SOC nicht im Notfall-Bereich (>= min_required)
            if net_in_optimal >= needed_kwh and soc >= min_required:
                return 0, "idle", (
                    f"Morgen: PV im Optimal-Fenster ({opt_start}:00-{opt_end}:00) "
                    f"ausreichend ({net_in_optimal:.1f} kWh >= {needed_kwh:.1f} kWh), warte")

            # Nicht genug PV im optimalen Fenster -> fruehes Laden noetig
            if soc < dyn_target - hyst:
                # Im optimalen Fenster mit genug PV -> reduzierter Strom
                if opt_start <= h_now <= opt_end and net_in_optimal >= needed_kwh * 1.2:
                    reduced_a = self.cc.get("reduced_charge_current_a", 20)
                    return reduced_a, "charging", (
                        f"Morgen: Optimal-Fenster, reduzierter Ladestrom {reduced_a}A "
                        f"um PV besser auszunutzen (SOC {soc:.1f}% -> {dyn_target:.0f}%)")
                # v3.0.9.10: Logtext unterscheidet zwischen zwei Ablehnungsgründen:
                # a) PV im Fenster ausreichend, aber SOC zu niedrig zum Warten (< min_required)
                # b) PV im Fenster wirklich nicht ausreichend
                if net_in_optimal >= needed_kwh:
                    reason = (
                        f"SOC {soc:.1f}% < min {min_required}%, kann nicht warten; "
                        f"Netto-Ueberschuss im Fenster ausreichend ({net_in_optimal:.1f} kWh >= {needed_kwh:.1f} kWh)")
                else:
                    reason = (
                        f"Netto-Ueberschuss nicht ausreichend im Optimal-Fenster "
                        f"({net_in_optimal:.1f} kWh < {needed_kwh:.1f} kWh)")
                return max_a, "charging", f"Morgen: {reason}, fruehes Laden noetig"

        # ── 6. Optimal-Fenster: Prognose-basierte Stundensteuerung (v3.0.11) ────
        # Kein Filter, keine Quantisierung, keine Glättung.
        # Strom wird beim Fenstereintritt und zu jeder vollen Stunde neu gesetzt,
        # basierend auf dem prognostizierten Netto-Überschuss der nächsten Stunde.
        # Defizit aus Vorjahr-Stunde (Ist < Plan) wird auf Reststunden verteilt.
        #
        # Innerhalb der Stunde: nur SOC vs. dyn_target wird überwacht.
        # Wenn SOC > dyn_target -> sofort auf min_charge_current.
        h_now = datetime.now().hour
        opt_start, opt_end = self._get_optimal_charge_window()
        nom_v = self.bat.get("voltage_nominal", 48.0)
        actual_v = max(self.state.battery_voltage, nom_v * 0.875, 42.0)
        max_a_opt = self.bat["max_charge_current"]
        min_charge_a_opt = float(self.bat.get("min_charge_current", 3.0))

        if opt_start <= h_now <= opt_end:
            # v3.0.11.4: Bei needs_full (Vollladung fällig) Optimal-Fenster komplett
            # ueberspringen -> decide() steuert den Volllade-Strom (max_a) und den
            # Trickle-Pfad direkt. Das Optimal-Fenster wuerde sonst den Strom auf
            # einen niedrigeren Plan-Wert reduzieren (z.B. 50A->15A) und danach
            # muss Trickle wieder hoch rampen (15A->20A).
            if not self._needs_full_charge():
                # SOC-Schutz: Ziel überschritten -> sofort Strom reduzieren
                if soc > dyn_target:
                    self._opt_plan_hour = -1  # Plan zurücksetzen, Ziel erreicht
                    return min_charge_a_opt, "trickle", (
                        f"Optimal-Fenster: SOC {soc:.1f}% > Ziel {dyn_target:.0f}% "
                        f"-> reduziere auf {min_charge_a_opt:.0f}A")

                fc_list = self.forecast.get_forecast()
                fc_by_hour_opt = {f.hour: f for f in fc_list}

                # Verbleibende Stunden im Fenster (inkl. aktueller Stunde)
                hours_left = max(1, opt_end - h_now + 1)

                # Bat-Wh-Total für Ist-Messung: EnergyAccumulator + Neustart-Basis
                bat_wh_total_now = self.state.bat_energy_today_wh + self._energy_base_bat

                # Neue Stunde? Neu planen wenn Stunde gewechselt oder noch kein Plan
                new_hour = self._opt_plan_hour != h_now

                if new_hour:
                    # Defizit der abgeschlossenen Vorjahr-Stunde berechnen
                    if self._opt_plan_hour >= 0:
                        # Tatsächlich geladene/entladene Wh seit Stundenbeginn (signed)
                        actual_wh = bat_wh_total_now - self._opt_bat_wh_snapshot
                        deficit_wh = self._opt_planned_wh - actual_wh
                        self._opt_carried_wh += deficit_wh
                        self.logger.info(
                            f"Optimal-Fenster H{self._opt_plan_hour:02d} abgeschlossen: "
                            f"Plan={self._opt_planned_wh:.0f}Wh, "
                            f"Ist={actual_wh:.0f}Wh, "
                            f"Defizit={deficit_wh:+.0f}Wh, "
                            f"Übertrag={self._opt_carried_wh:+.0f}Wh")

                    # Prognose-Netto-Überschuss der aktuellen Stunde [Wh]
                    fc_now = fc_by_hour_opt.get(h_now)
                    forecast_surplus_wh = max(0.0, fc_now.net_kwh * 1000.0) if fc_now else 0.0

                    # Fehlende Energie bis Ziel-SOC, gleichmäßig auf Reststunden verteilt.
                    # Begrenzt durch den Prognose-Überschuss dieser Stunde:
                    # Nie mehr anfordern als PV liefert, nie weniger als nötig.
                    missing_wh = max(0.0, (dyn_target - soc) / 100.0 * self.bat["capacity_kwh"] * 1000.0)
                    needed_wh = missing_wh / max(hours_left, 1)

                    # Defizit-Anteil aus Vorjahr-Stunden gleichmäßig verteilen
                    deficit_share_wh = self._opt_carried_wh / max(hours_left, 1)

                    # Geplante Ladeenergie: benötigte Energie + Defizit-Ausgleich,
                    # nach oben begrenzt durch Prognose-Überschuss.
                    planned_wh = min(forecast_surplus_wh, needed_wh + deficit_share_wh)
                    # Sicherheit: nie weniger als Defizit-Anteil allein (Aufholen sicherstellen)
                    planned_wh = max(planned_wh, min(deficit_share_wh, forecast_surplus_wh))

                    # Ladestrom: E[Wh] / t[1h] / U[V] = I[A]
                    planned_a = planned_wh / actual_v
                    charge_a = max(min_charge_a_opt, min(max_a_opt, planned_a))

                    # Plan für diese Stunde speichern
                    self._opt_plan_hour = h_now
                    self._opt_planned_wh = planned_wh
                    self._opt_bat_wh_snapshot = bat_wh_total_now
                    self._opt_setpoint_a = charge_a

                    self.logger.info(
                        f"Optimal-Fenster H{h_now:02d} neuer Plan: "
                        f"Prognose={forecast_surplus_wh:.0f}Wh, "
                        f"Bedarf={needed_wh:.0f}Wh/h ({missing_wh:.0f}Wh/{hours_left}h), "
                        f"Defizitanteil={deficit_share_wh:+.0f}Wh, "
                        f"Plan={planned_wh:.0f}Wh -> {charge_a:.1f}A")
                else:
                    charge_a = self._opt_setpoint_a

                return charge_a, "charging", (
                    f"Optimal-Fenster H{h_now:02d}: {charge_a:.0f}A "
                    f"(Plan {self._opt_planned_wh:.0f}Wh, "
                    f"Übertrag {self._opt_carried_wh:+.0f}Wh, "
                    f"SOC {soc:.1f}%→{dyn_target:.0f}%)")

        # ── 6. Nachmittag außerhalb Optimal-Fenster: SOC < Ziel -> max laden ──
        # Keine surplus-Abhängigkeit außerhalb des Optimal-Fensters.
        # Die bisherige surplus_w-Schwelle (200W) verursachte ständiges Flackern
        # zwischen Trickle (3A) und PV-Überschuss-Laden wenn PV um die Schwelle
        # schwankte. Victron ESS im Selbstverbrauchsmodus lädt nie aus dem Netz
        # wenn kein PV-Überschuss da ist  -  DVCC und ESS begrenzen den Strom
        # automatisch auf das, was PV tatsächlich liefert.
        if soc < dyn_target - hyst:
            # v3.0.14.2 Fix: Hysterese-Marge ergaenzt (analog Morgen-Block Zeile
            # ~630 und dem dokumentierten Hysterese-Muster oben bei "Ziel
            # bereits erreicht"). Vorher stand hier nur "soc < dyn_target" -
            # ein Bruchteil-Prozent-Rest (dyn_target ist Float, Log rundet nur
            # die Anzeige) reichte, um vollen Ramp auf max_a auszuloesen, der
            # eine Minute spaeter durch das naechste SOC-Update schon wieder
            # obsolet war (Log 2026-07-09 16:01-16:03: 27A -> 7A -> 3A, drei
            # Writes fuer ein SOC-Delta von deutlich unter 1%).
            # v3.0.14.0 (optional, Default AUS - siehe IDEA_AFTERNOON_NO_RAMP.md):
            # Kurz vor Sonnenuntergang verhindert selbst max_a den SOC-Abfall durch
            # Abendverbrauch laut Log-Analyse nicht mehr - Hochrampen bringt in
            # diesem Fenster nichts, kostet aber einen unnoetigen Modbus-Write.
            # Statt auf max_a zu rampen: aktuellen Strom halten (target_a=-1,
            # gleiche "kein Write"-Konvention wie winter_pause) bis Sonnenuntergang.
            if self.cc.get("afternoon_no_ramp_enabled", False):
                _, sunset, _ = self.forecast._calculate_sun_times(date.today())
                h_now_dec = datetime.now().hour + datetime.now().minute / 60.0
                before_sunset_h = sunset - h_now_dec
                threshold_h = self.cc.get("afternoon_no_ramp_before_sunset_h", 3.5)
                # v3.0.14.x Fix: keine Untergrenze bei 0.0 mehr. _is_night() rundet
                # den Nachtbeginn auf volle Stunden auf (math.ceil(sunset)), daher
                # gilt decide() bis zu ~1h NACH dem praezisen Sonnenuntergang noch
                # als "Tag". In diesem Rest-Fenster wurde before_sunset_h negativ,
                # die Bedingung schlug fehl und der Code fiel zurueck auf volles
                # Rampen auf max_a - exakt in dem Moment, den die Funktion
                # eigentlich abdecken soll (Log 2026-07-08 21:50: Write auf 50A,
                # 10 Min. spaeter durch _is_night() wieder auf 3A). Sobald
                # _is_night() greift, gibt Block 1 ohnehin schon vorher zurueck,
                # daher ist eine offene Untergrenze hier unkritisch.
                if before_sunset_h <= threshold_h:
                    return -1, "afternoon_hold", (
                        f"Nachmittag: SOC {soc:.1f}% < Ziel {dyn_target:.0f}%, "
                        f"aber {before_sunset_h:.1f}h vor Sonnenuntergang "
                        f"(<= {threshold_h:.1f}h) -> kein Hochrampen, "
                        f"halte aktuellen Ladestrom (afternoon_no_ramp_enabled)")
            return max_a, "charging", (
                f"Nachmittag: SOC {soc:.1f}% < Ziel {dyn_target:.0f}% "
                f"-> lade mit {max_a:.0f}A")

        # ── 7. Warten ─────────────────────────────────────────
        return 0, "idle", (
            f"Warte auf PV-Ueberschuss "
            f"(SOC {soc:.1f}%, PV {pv_w:.0f} W [glatt], Last {load_w:.0f} W [glatt])")
    def _ramp(self, target_a: float) -> float:
        """Sanftes Rampen des Ladestroms (+/- ramp_step A pro Zyklus).

        Ausnahme Nacht (vor Sonnenaufgang / nach Sonnenuntergang): kein PV-Fluss,
        daher kein Rampen notwendig -> Zielwert wird direkt gesetzt. Spart
        Modbus-Writes beim Uebergang in/aus FULL_CHARGE in der Dunkelphase, ohne
        die Rampen-Daempfung tagsueber (PV-Schwankungen) zu beeinflussen.
        """
        sunrise, sunset, _ = self.forecast._calculate_sun_times(date.today())
        h_now = datetime.now().hour + datetime.now().minute / 60.0
        is_night = h_now < sunrise or h_now > sunset
        if is_night:
            self._ramp_current = target_a
            return self._ramp_current
        step = self.cc.get("current_ramp_step", 5)
        if target_a > self._ramp_current:
            self._ramp_current = min(self._ramp_current + step, target_a)
        elif target_a < self._ramp_current:
            self._ramp_current = max(self._ramp_current - step, target_a)
        return self._ramp_current

    def _smooth_required_a(self, required_a: float) -> float:
        """Gleitender Mittelwert ueber N Zyklen fuer required_a.

        Verhindert Stufenwechsel durch langsamen hours_left-Drift am
        Ende des Optimal-Fensters. Konfigurierbar via
        charging.required_a_smooth_window (Default: 3 Zyklen ~ 3 Minuten).
        """
        self._required_a_history.append(required_a)
        return sum(self._required_a_history) / len(self._required_a_history)

    def _simulate_hour(self, h: int, fc: HourlyForecast, soc_sim: float,
                       needs_full: bool, pv_rem_total: float, night_cons: float,
                       is_forecast: bool = True,
                       floor_soc: Optional[float] = None) -> tuple[str, float, float]:
        """
        Simuliert EINE Stunde mit der exakten decide()-Logik (v3.0.3).
        Gibt (action, current_a, new_soc_sim) zurueck.

        v3.0.3: floor_soc beruecksichtigt evcc MinSoc-Sperre (Reg 2901).
                Wenn evcc den MinSoc angehoben hat, kann der Akku physikalisch
                nicht unter diesen Wert entladen  -  die Simulation muss das
                abbilden, sonst sind die projizierten SOC-Werte zu niedrig.
        """
        cap = self.bat["capacity_kwh"]
        min_soc = self.bat["min_soc"]
        # v3.0.3: effektiver SOC-Boden = konfigurierter min_soc oder evcc-MinSoc
        floor_soc = floor_soc if floor_soc is not None else min_soc
        max_soc = self.bat["max_soc"]
        nom_v = self.bat.get("voltage_nominal", 48.0)
        max_a = self.bat["max_charge_current"]
        hyst = self.cc.get("soc_hysteresis", 2)
        # v3.0.9: Dynamisch ab Sonnenaufgang
        sunrise, _, _ = self.forecast._calculate_sun_times(date.today())
        morn_s = int(sunrise)
        morn_e = morn_s + self.cc.get("morning_delay_h", 4)

        # Dynamisches target_soc fuer die Simulation (v3.0.0)
        dyn_target = self._calculate_target_soc()
        min_required = max(min_soc, self.cc.get("emergency_charge_soc", 25))

        action = "idle"
        current_a = 0.0
        min_charge_a = float(self.bat.get("min_charge_current", 3.0))

        # Maximale Ladeenergie pro Stunde durch Strombegrenzung
        max_charge_kwh = max_a * nom_v / 1000.0  # z.B. 50A * 48V / 1000 = 2.4 kWh

        # Wenn SOC < min_soc und PV-Ueberschuss vorhanden: Sofort laden
        # (Notladung / State-12-Simulation im Ladeplan)
        if soc_sim < min_soc and fc.net_kwh > 0:
            action = "charging"
            current_a = max_a
            charge_kwh = min(fc.net_kwh, max_charge_kwh)
            soc_sim = min(max_soc, soc_sim + (charge_kwh / cap) * 100)
            return action, current_a, soc_sim
        # Wenn SOC < min_soc und KEIN PV-Ueberschuss: weiter zum Notfall-SOC-Block.
        # Bei soc <= floor_soc friert der SOC ein (Victron ESS, s.u.).

        # Notfall-SOC
        if soc_sim <= self.cc.get("emergency_charge_soc", 25):
            if fc.net_kwh > 0:
                # PV-Ueberschuss: Laden moeglich
                action = "charging"
                current_a = max_a
                charge_kwh = min(fc.net_kwh, max_charge_kwh)
                soc_sim = min(max_soc, soc_sim + (charge_kwh / cap) * 100)
            else:
                # Kein PV-Ueberschuss und SOC <= emergency_charge_soc (= floor_soc):
                # Victron ESS sperrt Batterieentladung unter Reg 2901  -  Verbraucher
                # werden aus dem Netz gespeist, SOC bleibt konstant.
                # State 11 (SOC < MinSOC): Entladen gesperrt.
                # State 12 (Minimal-Ladung aus Netz): erhoeht SOC nicht nennenswert,
                # wird in der Simulation nicht modelliert (kein PV-Ueberschuss-Pfad).
                action = "discharging"
                current_a = 0.0
                if soc_sim > floor_soc:
                    deficit = max(0.0, fc.consumption_kwh - fc.pv_kwh)
                    soc_sim = max(floor_soc, soc_sim - (deficit / cap) * 100)
                # soc_sim <= floor_soc: SOC eingefroren
            return action, current_a, soc_sim

        # ── Vollladungs-Pfad (v3.0.13.4) ──────────────────────────────────────
        # An decide() Block 3 gekoppelt: decide() erzwingt max_a immer dann, wenn
        # dyn_target >= 98.0 - UNABHAENGIG davon, ob dieser Zielwert durch das
        # Vollladungs-Intervall (_needs_full_charge) ODER durch hohen Nachtverbrauch
        # zustande kommt (_calculate_target_soc deckelt beide Faelle auf 98 %).
        # Bisher gate-te die Simulation ausschliesslich auf needs_full (reines
        # Intervall-Kriterium). An Tagen, an denen der 98-%-Zielwert nur vom
        # Nachtverbrauch getrieben ist (needs_full == False), lief die Simulation
        # deshalb in die normale Optimal-Fenster-/Deficit-Logik und zeigte PAUSE
        # bzw. eine 20-A-Rampe, waehrend die Realsteuerung durchgaengig max_a hielt
        # (Symptom: SOC-Trajektorie und Aktion im Ladeplan passten nicht zur
        # Realitaet).
        #
        # Die Simulation kann die Batteriespannung nicht abbilden, spiegelt aber das
        # SOC-Kriterium exakt: max_a bis soc_sim >= 98 %, danach Trickle-Hold. Der
        # Spannungsanteil (U >= full_charge_min_voltage) von decide()s
        # full_charge_complete entfaellt in der Projektion notgedrungen -> der Plan
        # kann den Uebergang max_a -> Trickle geringfuegig frueher zeigen als die
        # Realsteuerung, die zusaetzlich auf 55 V wartet. Die alte 96-%-Grenze
        # (max_soc - hyst) entfaellt; sie hatte denselben 2-%-Frueh-Uebergang wie
        # der in v3.0.13.1 in decide() beseitigte Luecken-Bug.
        # needs_full bleibt referenziert (Intervall-Fall ist Teilmenge von >=98 %).
        target_full = dyn_target >= 98.0 or needs_full
        full_soc_reached = soc_sim >= 98.0

        if target_full and full_soc_reached:
            # Trickle-Hold (Cellbalancing) - decide() haelt hier trickle_current.
            # Die Simulation kennt _balancing_hold_until nicht und modelliert die
            # Haltephase vereinfacht: sobald soc_sim >= 98 % -> trickle_current.
            trickle_a = float(self.bat.get("trickle_current", 5))
            trickle_kwh = trickle_a * nom_v / 1000.0
            deficit = max(0.0, fc.consumption_kwh - fc.pv_kwh)
            new_soc = soc_sim + ((trickle_kwh - deficit) / cap) * 100
            new_soc = max(floor_soc, min(max_soc, new_soc))
            return "full_charge", trickle_a, new_soc

        if target_full and not full_soc_reached:
            if fc.net_kwh > 0:
                # PV-Ueberschuss: Akku laden (Vollladung/Balancing)
                action = "full_charge"
                current_a = max_a
                charge_kwh = min(fc.net_kwh, max_charge_kwh)
                soc_sim = min(max_soc, soc_sim + (charge_kwh / cap) * 100)
            else:
                # Kein PV-Ueberschuss: Setpoint bleibt max_a (decide() schreibt ihn
                # ebenfalls in Block 3), aber ESS laedt nur aus PV, nicht aus dem
                # Netz. Ohne PV deckt der Akku die Last -> SOC sinkt um das Defizit,
                # begrenzt durch floor_soc (Reg 2901 Entladesperre). Entspricht der
                # naechtlichen VOLLLADUNG-mit-SOC-Abfall-Darstellung der History.
                action = "full_charge"  # Soll-Aktion bleibt (Setpoint wird geschrieben)
                current_a = max_a       # Setpoint bleibt, aber Netz laedt nicht in Batterie
                if soc_sim > floor_soc:
                    deficit = max(0.0, fc.consumption_kwh - fc.pv_kwh)
                    soc_sim = max(floor_soc, soc_sim - (deficit / cap) * 100)
                # soc_sim <= floor_soc: SOC eingefroren, kein Deficit abziehen
            return action, current_a, soc_sim

        def _apply_deficit(soc: float) -> tuple[str, float, float]:
            """Berechnet Deficit und setzt action: discharging wenn Ueberschuss negativ, sonst idle.

            Bei soc <= floor_soc (Reg 2901 ESS MinimumSocLimit):
            Victron ESS sperrt die Batterieentladung  -  Verbraucher werden aus dem Netz
            gespeist, Batterie weder geladen noch entladen. SOC bleibt konstant.
            max(floor_soc, ...) bildet dieses Verhalten ab.

            v3.0.9.24: Reg. 2705 steht auch im idle-Zustand auf mindestens
            min_charge_current (3 A). Bei soc > floor_soc fliesst dieser Strom
            tatsaechlich in die Batterie (~1 %/h bei 3 A / 48 V / 100 Ah).
            Die Simulation bildet das ab: current_a = min_charge_a, SOC steigt leicht.
            Ausnahme: soc <= floor_soc -> current_a = 0 (Entladesperre, kein Laden).
            """
            deficit = max(0.0, fc.consumption_kwh - fc.pv_kwh)
            act = "discharging" if fc.net_kwh < 0 else "idle"
            if soc > floor_soc:
                # Trickle: min_charge_a fliesst, netto SOC-Aenderung =
                # (min_charge_kwh - deficit_kwh), aber nie unter floor_soc.
                trickle_kwh = min_charge_a * nom_v / 1000.0
                new_soc = soc + ((trickle_kwh - deficit) / cap) * 100
                new_soc = max(floor_soc, new_soc)
                new_soc = min(max_soc, new_soc)  # v3.0.11.2: SOC nie ueber max_soc (Simulation)
                return act, min_charge_a, new_soc
            else:
                # SOC <= floor_soc: Entladesperre aktiv, SOC eingefroren
                return act, 0.0, soc

        # Morgen-Notladung in der Simulation (v3.0.4)
        # Wenn SOC unter min_required: Sofort laden bei Ueberschuss, aber nur
        # bis min_required. Danach greift die normale adaptive Planung.
        if morn_s <= h < morn_e and soc_sim < min_required:
            if fc.net_kwh > 0:
                action = "charging"
                current_a = max_a
                # Nur bis min_required laden, nicht hoeher
                needed_kwh = max(0.0, (min_required - soc_sim) / 100.0 * cap)
                charge_kwh = min(fc.net_kwh, needed_kwh, max_charge_kwh)
                soc_sim = min(min_required, soc_sim + (charge_kwh / cap) * 100)
                return action, current_a, soc_sim
            else:
                # Kein PV-Ueberschuss: Victron ESS sperrt Batterieentladung bei
                # soc <= floor_soc  -  SOC friert ein, Verbraucher aus Netz gespeist.
                deficit = max(0.0, fc.consumption_kwh - fc.pv_kwh)
                soc_sim = max(floor_soc, soc_sim - (deficit / cap) * 100)
                return "discharging", 0.0, soc_sim

        # Ziel erreicht?
        # v3.0.9.10: Asymmetrische Hysterese analog zu decide()  -  Simulation stoppt
        # erst bei soc_sim >= dyn_target (nicht bei dyn_target - hyst), damit
        # Ladeplan und Realsteuerung konsistent bleiben.
        if soc_sim >= dyn_target:
            action, current_a, soc_sim = _apply_deficit(soc_sim)
            # Ping-Pong-Schutz: Deficits kleiner als Hysterese-Energie klemmen SOC
            hyst_kwh = hyst / 100.0 * cap
            if fc.net_kwh >= -hyst_kwh:
                soc_sim = max(soc_sim, dyn_target - hyst)
            # v3.0.3: Nie unter floor_soc fallen
            soc_sim = max(soc_sim, floor_soc)
            # v3.0.11.2: Nie ueber max_soc steigen (Simulation)
            soc_sim = min(soc_sim, max_soc)
            return action, current_a, soc_sim

        # Morgen-Verzoegerung / Adaptive Fenster (v3.0.0)
        # Erweiterter Bereich: morn_s bis max(morn_e, opt_start) damit keine Luecke
        # zwischen Morgen-Fenster-Ende und opt_start entsteht.
        opt_start, opt_end = self._get_optimal_charge_window()
        effective_morn_e = max(morn_e, opt_start)
        if morn_s <= h < effective_morn_e:

            # Wenn Stunde noch vor dem optimalen Fenster und SOC nicht kritisch -> warte
            # v3.0.9.27: Nur warten wenn Optimal-Fenster den Netto-Ueberschuss tatsaechlich
            # liefert  -  spiegelt decide() exakt (pv_in_optimal -> net_in_optimal Fix).
            if h < opt_start:
                fc_list_sim = self.forecast.get_forecast()
                net_in_opt = sum(max(0.0, f2.net_kwh) for f2 in fc_list_sim
                                 if opt_start <= f2.hour <= opt_end)
                needed = max(0.0, (dyn_target - soc_sim) / 100.0 * cap)
                if net_in_opt >= needed and soc_sim >= min_required:
                    action, current_a, soc_sim = _apply_deficit(soc_sim)
                    return action, current_a, soc_sim

            # Fruehes Laden noetig oder im optimalen Fenster
            if fc.net_kwh > 0:
                action = "charging"
                # Adaptive Reduktion im optimalen Fenster
                if opt_start <= h <= opt_end:
                    reduced_a = self.cc.get("reduced_charge_current_a", 20)
                    current_a = min(fc.net_kwh * 1000 / nom_v, reduced_a)
                else:
                    current_a = min(fc.net_kwh * 1000 / nom_v, max_a)
                charge_kwh = min(fc.net_kwh, current_a * nom_v / 1000.0)
                soc_sim = min(dyn_target, soc_sim + (charge_kwh / cap) * 100)
            else:
                action, current_a, soc_sim = _apply_deficit(soc_sim)
            return action, current_a, soc_sim

        # PV-Ueberschuss mit adaptivem Strom (v3.0.0)
        if fc.net_kwh > 0:
            action = "charging"
            if opt_start <= h <= opt_end:
                # Im optimalen Fenster: reduzierter Strom wenn moeglich
                reduced_a = self.cc.get("reduced_charge_current_a", 20)
                current_a = min(fc.net_kwh * 1000 / nom_v, reduced_a)
            else:
                # Ausserhalb Optimal-Fenster: konsistent mit decide()
                # Kein fc.net_kwh-Cap: ESS/DVCC begrenzen physikalisch,
                # decide() setzt ebenfalls max_a wenn soc < dyn_target.
                if soc_sim < dyn_target:
                    current_a = max_a
                else:
                    # Ziel bereits erreicht (defensiv, sollte durch
                    # soc_sim >= dyn_target-Block oben abgefangen sein).
                    current_a = min_charge_a
            charge_kwh = min(fc.net_kwh, current_a * nom_v / 1000.0)
            # Deckelung auf dyn_target (nicht max_soc)
            soc_sim = min(dyn_target, soc_sim + (charge_kwh / cap) * 100)
        else:
            action, current_a, soc_sim = _apply_deficit(soc_sim)

        return action, current_a, soc_sim
    def build_schedule(self) -> list:
        """
        Stundlicher Ladeplan:
        - Vergangene Stunden: Tatsaechliche Werte aus history_buffer (heute)
        - Zukuenftige Stunden: Prognose via _simulate_hour() mit exakter decide()-Logik

        BUGFIX: Jede Stunde ist entweder Vergangenheit ODER Zukunft.
        Keine Stunde kann verschwinden oder doppelt auftreten.
        """
        fc_list = self.forecast.get_forecast()
        today_iso = date.today().isoformat()
        now = datetime.now()
        now_h = now.hour
        now_m = now.minute

        result = []

        # --- VERGANGENE STUNDEN: History (nur von heute) ---
        today_history = [h for h in self.state.history_buffer if h.date_iso == today_iso]
        history_by_hour = {h.hour: h for h in today_history}

        # Alle Stunden bis now_h als Vergangenheit behandeln,
        # aber die aktuelle Stunde nur wenn sie fast vorbei ist (>= 55 Min).
        for h in range(now_h + 1):
            if h == now_h and now_m < 55:
                continue  # Aktuelle Stunde noch laufend -> Zukunft

            hist = history_by_hour.get(h)
            if hist:
                result.append({
                    "hour": h,
                    "pv_kwh": hist.pv_kwh,
                    "consumption_kwh": hist.consumption_kwh,
                    "surplus_kwh": round(hist.surplus_kwh, 3),
                    "action": hist.action,
                    "charge_current_a": round(hist.charge_current_a, 1),
                    "projected_soc": round(hist.soc_end, 1),
                    "is_past": True,
                    "is_actual": True,
                })
            elif h < now_h:
                # Luecke in der History (z.B. nach Neustart oder Ausfall)
                gap_soc = next(
                    (history_by_hour[hh].soc_end for hh in range(h, now_h + 1)
                     if hh in history_by_hour and not (hh == now_h and now_m < 55)),
                    self.state.soc
                )
                result.append({
                    "hour": h,
                    "pv_kwh": 0.0,
                    "consumption_kwh": 0.0,
                    "surplus_kwh": 0.0,
                    "action": "unknown",
                    "charge_current_a": 0,
                    "projected_soc": round(gap_soc, 1),
                    "is_past": True,
                    "is_actual": False,
                })

        # --- ZUKUENFTIGE STUNDEN: Prognose via _simulate_hour ---
        # Startpunkt: letzter tatsaechlicher SOC aus History.
        # Wenn die aktuelle Stunde schon als Vergangenheit laeuft, nimm deren SOC.
        last_actual_soc = self.state.soc
        past_entries = [h for h in today_history
                        if h.hour < now_h or (h.hour == now_h and now_m >= 55)]
        if past_entries:
            last_actual_soc = past_entries[-1].soc_end

        soc_sim = last_actual_soc
        needs_full = self._needs_full_charge()
        pv_rem_total = self.forecast.pv_remaining_kwh()
        night_cons = self.forecast.night_consumption_kwh()
        fc_by_hour = {f.hour: f for f in fc_list}

        # Effektiver SOC-Boden fuer Simulation:
        # Reg 2901 (ESS MinimumSocLimit) ist die harte physikalische Untergrenze,
        # die Victron in ESS State 11/12 durchsetzt. Unabhaengig von evcc oder
        # dem konfigurierten bat.min_soc darf die Simulation nicht darunter sinken.
        floor_soc = self.state.evcc_min_soc if self.state.evcc_min_soc > 0 else self.bat["min_soc"]
        self.logger.debug(f"[SCHEDULE] floor_soc={floor_soc:.1f}% (evcc_min_soc={self.state.evcc_min_soc:.1f}%, bat.min_soc={self.bat['min_soc']:.1f}%)")

        for h in range(now_h, 24):
            # Aktuelle Stunde nur als Zukunft wenn sie noch laeuft (< 55 Min)
            if h == now_h and now_m >= 55:
                continue

            fc = fc_by_hour.get(h)
            if not fc:
                avg_cons = self.cc.get("avg_daily_consumption_kwh", 8.0) / 24
                fc = HourlyForecast(hour=h, pv_kwh=0.0, consumption_kwh=avg_cons, net_kwh=-avg_cons)

            action, current_a, soc_sim = self._simulate_hour(
                h, fc, soc_sim, needs_full, pv_rem_total, night_cons,
                floor_soc=floor_soc
            )

            # Geplanter Stromfluss: universelle Formel fuer alle Stunden:
            #   planned_current_a = min(surplus_current_a, effective_setpoint_a)
            # effective_setpoint_a = max(current_a, min_charge_current)
            # Begruendung: Reg. 2705 steht immer auf mindestens min_charge_current
            # (write_charge_current() clampt auf min_charge_current als Untergrenze).
            # v3.0.9.24: _simulate_hour() gibt bei idle (soc > floor_soc) bereits
            # current_a = min_charge_a zurueck, sodass max() hier idempotent ist.
            # Ausnahme SOC <= floor_soc: _simulate_hour() gibt current_a=0 zurueck
            # (Victron ESS Entladesperre)  -  effective_setpoint_a bleibt 0 -> 0 A.
            nom_v = self.bat.get("voltage_nominal", 48.0)
            min_charge_a = float(self.bat.get("min_charge_current", 3.0))
            surplus_current_a = fc.net_kwh * 1000.0 / nom_v
            # Entladesperre: current_a==0 von _simulate_hour() durchreichen (nicht auf 3A anheben)
            effective_setpoint_a = 0.0 if current_a == 0.0 else max(current_a, min_charge_a)
            if surplus_current_a >= 0:
                planned_current_a = round(min(surplus_current_a, effective_setpoint_a), 1)
            else:
                planned_current_a = round(surplus_current_a, 1)

            # v3.0.11: Für die laufende Stunde im Optimal-Fenster den echten
            # Stunden-Setpoint aus _opt_setpoint_a anzeigen statt des simulierten
            # Wertes. Simulation kennt keine Defizit-Korrekturen aus Vorjahr-Stunden.
            is_current_hour = (h == now_h and now_m < 55)
            if is_current_hour and self._opt_plan_hour == now_h:
                planned_current_a = round(self._opt_setpoint_a, 1)
                if action != "trickle":
                    action = "charging"

            result.append({
                "hour": h,
                "pv_kwh": fc.pv_kwh,
                "consumption_kwh": fc.consumption_kwh,
                "surplus_kwh": round(fc.net_kwh, 3),
                "action": action,
                "charge_current_a": planned_current_a,
                "projected_soc": round(soc_sim, 1),
                "is_past": False,
                "is_actual": False,
            })

            pv_rem_total = max(0.0, pv_rem_total - fc.pv_kwh)

        return result

    def _update_history(self):
        """
        Speichert stuendliche Werte im History-Ringpuffer.
        Berechnet stuendliche Energiedifferenzen aus den Tageskumulativen.

        KORREKTUR: pv_kwh/consumption_kwh speichern jetzt die GESAMTSUMME
        der Stunde (seit Stundenbeginn), nicht nur den Diff des letzten Updates.
        Zusaetzlich: Energie-Basis nach Neustart wird beruecksichtigt.

        v3.0.9.22: charge_current_a wird aus integriertem Batteriestrom (Reg. 842)
        berechnet: bat_energy_wh / nom_v -> mittlerer Strom [A], signed.
        Waehrend der laufenden Stunde: laufend aktualisiert.
        Beim Stundenabschluss: Endwert eingefroren.

        Erzeugt HourlyHistory-Dataclass-Objekte (keine Dictionaries!)
        """
        now = datetime.now()
        today_iso = now.date().isoformat()
        current_hour = now.hour
        nom_v = self.bat.get("voltage_nominal", 48.0)

        # Letzten Eintrag suchen (gleiche Stunde oder vorherige)
        last_entry = None
        for _, h in enumerate(self.state.history_buffer):
            if h.date_iso == today_iso and h.hour <= current_hour:
                last_entry = h

        # Energie-Totals: aus EnergyAccumulator + persistierter Basis nach Neustart
        pv_total   = self.state.pv_energy_today_kwh   + self._energy_base_pv
        load_total = self.state.load_energy_today_kwh + self._energy_base_load
        bat_wh_total = self.state.bat_energy_today_wh + self._energy_base_bat  # signed Wh + Neustart-Basis

        def _bat_current_a(bat_wh_hour: float, elapsed_h: float) -> float:
            """Mittlerer Batteriestrom [A] aus Wh-Integral. signed: + Laden, - Entladen."""
            if elapsed_h < 1e-6:
                return 0.0
            return round(bat_wh_hour / (nom_v * elapsed_h), 1)

        if last_entry and last_entry.hour == current_hour:
            # Gleiche Stunde: Update mit GESAMTDIFF seit Stundenbeginn
            # (nicht nur Diff seit letztem Update!)
            pv_hour_total = max(0.0, pv_total - last_entry._hour_start_pv_total)
            cons_hour_total = max(0.0, load_total - last_entry._hour_start_cons_total)
            bat_wh_hour = bat_wh_total - last_entry._hour_start_bat_wh

            last_entry.pv_kwh = round(pv_hour_total, 3)
            last_entry.consumption_kwh = round(cons_hour_total, 3)
            last_entry.surplus_kwh = round(pv_hour_total - cons_hour_total, 3)
            last_entry.action = self.state.charge_mode
            last_entry.bat_energy_wh = round(bat_wh_hour, 1)
            # Laufender Mittelwert: elapsed = aktuelle Minute / 60
            # v3.0.11.1: Unter 5 Minuten ist elapsed_h so klein, dass bat_wh/elapsed
            # einen riesigen Phantomstrom ergibt (z.B. -69 A bei 00:00+30s).
            # Anzeige in den ersten 5 Minuten ist ohnehin nicht aussagekraeftig -> 0.0.
            elapsed_h = now.minute / 60.0 + now.second / 3600.0
            last_entry.charge_current_a = (
                _bat_current_a(bat_wh_hour, elapsed_h) if elapsed_h >= 5 / 60 else 0.0
            )
            last_entry.soc_end = round(self.state.soc, 1)
            # Aktuelle Kumulativwerte fuer naechsten Update speichern
            last_entry._raw_pv_total   = pv_total
            last_entry._raw_cons_total = load_total
            last_entry._raw_bat_wh     = bat_wh_total

        elif last_entry and last_entry.hour < current_hour:
            # NEUE STUNDE: Letzten Eintrag der VORHERIGEN Stunde abschliessen
            pv_hour_total = max(0.0, last_entry._raw_pv_total - last_entry._hour_start_pv_total)
            cons_hour_total = max(0.0, last_entry._raw_cons_total - last_entry._hour_start_cons_total)
            bat_wh_hour = last_entry._raw_bat_wh - last_entry._hour_start_bat_wh
            last_entry.pv_kwh = round(pv_hour_total, 3)
            last_entry.consumption_kwh = round(cons_hour_total, 3)
            last_entry.surplus_kwh = round(pv_hour_total - cons_hour_total, 3)
            last_entry.bat_energy_wh = round(bat_wh_hour, 1)
            # Abgeschlossene Stunde: Mittelwert ueber volle Stunde (elapsed_h = 1.0)
            last_entry.charge_current_a = _bat_current_a(bat_wh_hour, 1.0)
            last_entry.soc_end = round(self.state.soc, 1)

            # Neuen Eintrag fuer aktuelle Stunde erstellen
            new_entry = HourlyHistory(
                date_iso=today_iso,
                hour=current_hour,
                pv_kwh=0.0,
                consumption_kwh=0.0,
                surplus_kwh=0.0,
                action=self.state.charge_mode,
                charge_current_a=0.0,   # wird laufend aus bat_energy_wh berechnet
                soc_start=round(self.state.soc, 1),
                soc_end=round(self.state.soc, 1),
                is_actual=True,
            )
            # Stundenbeginn-Kumulativwerte speichern (inkl. Energie-Basis)
            new_entry._hour_start_pv_total   = pv_total
            new_entry._hour_start_cons_total  = load_total
            new_entry._hour_start_bat_wh      = bat_wh_total
            new_entry._raw_pv_total   = pv_total
            new_entry._raw_cons_total = load_total
            new_entry._raw_bat_wh     = bat_wh_total
            self.state.history_buffer.append(new_entry)

        else:
            # Erster Eintrag des Tages
            new_entry = HourlyHistory(
                date_iso=today_iso,
                hour=current_hour,
                pv_kwh=0.0,
                consumption_kwh=0.0,
                surplus_kwh=0.0,
                action=self.state.charge_mode,
                charge_current_a=0.0,
                soc_start=round(self.state.soc, 1),
                soc_end=round(self.state.soc, 1),
                is_actual=True,
            )
            new_entry._hour_start_pv_total   = pv_total
            new_entry._hour_start_cons_total  = load_total
            new_entry._hour_start_bat_wh      = bat_wh_total
            new_entry._raw_pv_total   = pv_total
            new_entry._raw_cons_total = load_total
            new_entry._raw_bat_wh     = bat_wh_total
            self.state.history_buffer.append(new_entry)

        # DEBUG: Logge letzte Stunde fuer Vergleich mit VRM Portal
        if last_entry and last_entry.hour >= 0:
            self.logger.debug(
                f"History H{last_entry.hour:02d}: PV={last_entry.pv_kwh:.3f} kWh, "
                f"Cons={last_entry.consumption_kwh:.3f} kWh, "
                f"Surplus={last_entry.surplus_kwh:.3f} kWh, "
                f"BatStrom={last_entry.charge_current_a:+.1f} A, "
                f"SOC={last_entry.soc_end:.1f}%, "
                f"Action={last_entry.action}"
            )

        # Alte Eintraege entfernen (nur heute und gestern behalten)
        cutoff = (now.date() - timedelta(days=1)).isoformat()
        self.state.history_buffer = [
            h for h in self.state.history_buffer
            if h.date_iso >= cutoff
        ]

        if len(self.state.history_buffer) > 48:
            self.state.history_buffer = self.state.history_buffer[-48:]


    def run_cycle(self):
        """Fuehrt einen Regelzyklus aus (v3.0.0)."""

        # ── Mitternachts-Reset Balancing-Timer (v3.0.2) ───────────────────────
        # MUSS vor _update_history() laufen (v3.0.11.5):
        # _update_history() legt um 00:00 den ersten H00-Eintrag an und setzt
        # _hour_start_bat_wh = bat_wh_total. Wenn _energy_base_bat erst danach
        # zurueckgesetzt wird, traegt H00 die alte kumulative Tagesbasis als
        # Startwert — alle Folge-Updates subtrahieren dann von einem falschen
        # Ursprung und erzeugen Phantomstroeme (~148 A bei SOC 80%).
        # Reihenfolge: Reset -> _update_history() -> bat_wh_total = 0 + 0 = 0 korrekt.
        _today_iso = date.today().isoformat()
        if _today_iso != self._balancing_reset_date:
            self._balancing_hold_until = 0.0
            self._soc_98_reached_at    = None
            self._balancing_completed_today = False  # v3.0.13.3: neue Sperre, neuer Tag
            self._balancing_reset_date = _today_iso
            self._power_smoother.reset()  # Glättungspuffer bei Tageswechsel leeren
            self._energy_base_bat = 0.0  # Wh-Basis zurücksetzen
            # v3.0.11: Optimal-Fenster Plan zurücksetzen (neuer Tag, neues Fenster)
            self._opt_plan_hour = -1
            self._opt_planned_wh = 0.0
            self._opt_bat_wh_snapshot = 0.0
            self._opt_carried_wh = 0.0
            self._opt_setpoint_a = 0.0
            self.logger.info("Mitternachts-Reset: Balancing-Timer und Glättung zurueckgesetzt")

        self._update_history()

        # ── days_since_full_charge immer aktuell aus Datum berechnen ──────────
        if self.state.last_full_charge_date:
            try:
                d = date.fromisoformat(self.state.last_full_charge_date)
                self.state.days_since_full_charge = (date.today() - d).days
            except ValueError:
                pass

        # ── Auto-Reset Vollladung (v3.0.0, verschaerft v3.0.13) ────────────────
        # Wenn SOC >= 98% UND battery_voltage >= full_charge_min_voltage (Default
        # 55V) gemeinsam fuer mindestens eine Stunde anliegen -> days_since_full_charge
        # zuruecksetzen. Reiner SOC-Trigger reicht nicht: bei LiFePO4 ist die
        # Spannungskurve um 98% SOC sehr flach, ein knapper SOC-Peak ohne erreichte
        # Zellspannung ist kein abgeschlossener Balancing-Zyklus (CHANGELOG 2026-06-28).
        # Sofortiger Abbruch (kein Hysterese-Gnadenintervall) bei Unterschreiten
        # einer der beiden Bedingungen: Ziel ist hier "stabil erreicht", nicht
        # "einmal erreicht und knapp drunter geblieben".
        full_min_v = self.bat.get("full_charge_min_voltage", 55.0)
        soc_at_full = self.state.soc >= 98.0
        voltage_at_full = self.state.battery_voltage >= full_min_v
        if soc_at_full and voltage_at_full:
            if self._soc_98_reached_at is not None:
                # Timer laeuft -> Fortschritt pruefen (>=1h stabil -> Auto-Reset).
                elapsed = (datetime.now() - self._soc_98_reached_at).total_seconds()
                if elapsed >= 3600 and self.state.days_since_full_charge > 0:
                    self.logger.info(
                        f"Auto-Reset Vollladung: SOC >= 98% & U >= {full_min_v:.1f}V "
                        f"fuer {elapsed/60:.0f} Minuten "
                        f"-> days_since_full_charge auf 0 zurueckgesetzt")
                    self.state.last_full_charge_date = date.today().isoformat()
                    self.state.days_since_full_charge = 0
                    self._save_persistent()
                    self._soc_98_reached_at = None
            elif not self._balancing_completed_today:
                # Kein laufender Timer und heute noch kein abgeschlossener Zyklus
                # -> Auto-Reset-Timer + Cellbalancing-Haltezeit starten.
                self._soc_98_reached_at = datetime.now()
                # v3.0.1: Cellbalancing-Haltezeit starten. Nur wenn kein Countdown
                # laeuft ("== 0.0" reicht nicht: ein noch laufender Timer darf nicht
                # ueberschrieben werden, das wuerde ihn zuruecksetzen).
                hold_h = self.bat.get("balancing_hold_hours", 5)
                if self._balancing_hold_until <= time.monotonic():
                    self._balancing_hold_until = time.monotonic() + hold_h * 3600
            # else (_soc_98_reached_at is None UND _balancing_completed_today):
            # v3.0.13.5 - Cellbalancing heute bereits natuerlich abgeschlossen und
            # Auto-Reset-Timer bereits genullt, SOC/U liegen aber weiterhin
            # >=98%/55V. Kein neuer Timer, kein erneuter Reset -> No-op bis zum
            # Mitternachts-Reset von _balancing_completed_today.
            # (Fix TypeError: der alte gemeinsame else-Zweig rechnete
            # datetime.now() - None, sobald das Balancing an einem Tag mit dauerhaft
            # hoher Spannung abgeschlossen war; am 28.06. nur durch das Absinken von
            # U unter 55V zufaellig nie getriggert.)
        else:
            # v3.0.13: SOC ODER Spannung unter Trigger-Schwelle. Fuer den
            # 60-Min-Auto-Reset-Timer (_soc_98_reached_at) gilt sofortiger
            # Abbruch ohne Hysterese - ein Reset auf Basis eines instabilen
            # Peaks ist nicht erwuenscht.
            #
            # WICHTIG: _balancing_hold_until wird hier NICHT pauschal auf 0
            # gesetzt, sonst wuerde run_cycle() den laufenden Cellbalancing-Hold
            # noch vor decide() abwuergen, sobald SOC/U nur knapp unter die
            # 98%/55V-Trigger-Schwelle (statt unter die 0,1V-Hysterese-Schwelle)
            # fallen - decide() wuerde die Hysterese dann nie zu Gesicht
            # bekommen. Der eigentliche Hold-Abbruch inkl. Hysterese-Pruefung
            # geschieht zentral in decide() (siehe dort), das auch
            # _balancing_hold_until = 0.0 setzt, wenn die Hysterese-Schwelle
            # tatsaechlich unterschritten wird.
            if self._soc_98_reached_at is not None:
                self.logger.info(
                    f"Vollladung-Timer abgebrochen: SOC {self.state.soc:.1f}% "
                    f"(>=98% ben.) / U {self.state.battery_voltage:.1f}V "
                    f"(>={full_min_v:.1f}V ben.) nicht mehr gemeinsam erfuellt")
            self._soc_98_reached_at = None

        now = time.monotonic()
        min_dur = self._min_charge_duration_s

        # Sofort-Entscheidung bei kritischen Zustaenden (Sicherheit)
        force_new = False
        if self.state.soc <= self.cc.get("emergency_charge_soc", 25):
            force_new = True
        # SOC unter min_soc: keine Hysterese, sofort neu entscheiden.
        # Ohne das bleibt eine gecachte Nacht-Entscheidung bis zu 10 Minuten aktiv,
        # obwohl es laengst hell ist und PV-Ueberschuss vorhanden waere.
        if self.state.soc < self.bat["min_soc"] - 2:
            force_new = True
        elif self.state.soc < self.bat["min_soc"]:
            # Knapp unter min_soc: max. alle 2 Minuten neu entscheiden,
            # nicht bei jedem Zyklus (verhindert Oszillation durch Wolken/Lastspitzen)
            if not hasattr(self, '_min_soc_force_ts') or \
               (now - getattr(self, '_min_soc_force_ts', 0)) > 120:
                force_new = True
                self._min_soc_force_ts = now
        if self._needs_full_charge() and self.state.soc < self.bat["max_soc"] - self.cc.get("soc_hysteresis", 2):
            force_new = True

        # Massiver Export ins Netz -> sofort neu entscheiden, nicht 10 min warten.
        # Verhindert dass eine veraltete Trickle/Idle-Entscheidung eingefroren bleibt
        # während hunderte Watt unnötig eingespeist werden. (v3.0.9.20 Bugfix)
        if self.state.grid_power_w < -1000:
            force_new = True
            self.logger.debug(
                f"Massiver Export {self.state.grid_power_w:.0f}W -> force_new=True")

        # evcc-Statuswechsel (Start/Stop) -> sofort neu entscheiden.
        # Nach evcc-Stop fällt die Last schlagartig -> Überschuss steigt -> sofort laden.
        # Nach evcc-Start steigt die Last -> Ladestrom muss reduziert werden. (v3.0.9.20 Bugfix)
        evcc_now = self.state.evcc_active
        if evcc_now != getattr(self, "_last_evcc_active", evcc_now):
            force_new = True
            self.logger.info(
                f"evcc-Statuswechsel {'-> aktiv' if evcc_now else '-> gestoppt'} -> force_new=True")
        self._last_evcc_active = evcc_now

        # Neue Entscheidung nur wenn Hysterese abgelaufen oder forced
        if force_new or (now - self._last_decision_ts) >= min_dur:
            target_a, mode, reason = self.decide()
            self._last_decision_ts = now
            self._last_decision_result = (target_a, mode, reason)
        else:
            target_a, mode, reason = self._last_decision_result
            # Sonderfall: Balancing-Haltezeit laeuft -> decide() neu aufrufen damit
            # remaining_min jeden Zyklus aktuell berechnet wird (kein eingefrorener Countdown)
            if mode == "trickle" and self._balancing_hold_until > 0:
                target_a, mode, reason = self.decide()
            elif mode == "idle" and not reason.endswith("(Hysterese)"):
                # "(Hysterese)" nur bei idle anhaengen  -  signalisiert dass
                # die Warteentscheidung im Cache ist, nicht eine Lade-Entscheidung.
                reason = reason + " (Hysterese)"
        # Schreiben nur wenn sich der Sollwert tatsaechlich geaendert hat
        # v3.0.11: Einheitliche Rampe + Hysterese für alle Modi inkl. Optimal-Fenster.
        # Im Optimal-Fenster ändert sich target_a nur stündlich (neuer Plan) oder
        # beim SOC-Guard — die Rampe läuft schrittweise zum neuen Ziel, danach
        # ist ramped == target_a == last_written → kein weiterer Write (Hysterese 1A).
        # Außerhalb des Optimal-Fensters: identisches Verhalten.
        write_performed = False
        if target_a >= 0:
            # v3.0.11: Im Optimal-Fenster wird der Strom genau wie alle anderen
            # Modi gerampet. Der Zielstrom ändert sich nur stündlich (neuer Plan)
            # oder beim SOC-Guard — die Rampe dämpft den Sprung sanft.
            # Kein Sofort-Sprung mehr (war v3.0.10.7-Erbschaft für alten Hysterese-Bug).
            ramped = self._ramp(target_a)

            write_threshold = 1.0

            # Hysterese-Logik:
            # - Optimal-Fenster: Hysterese auf target_a (ändert sich nur stündlich).
            #   Die Rampe läuft schrittweise zum Ziel; geschrieben wird bei jedem
            #   Rampenschritt (ramped != last_written) bis Ziel erreicht.
            # - Alle anderen Modi: Hysterese auf gerampetem Wert.
            should_write = abs(ramped - self._last_written_ramped_a) >= write_threshold

            if should_write:
                if self.victron.set_max_charge_current(ramped):
                    # WICHTIG: _last_written_ramped_a muss den tatsaechlich geschriebenen
                    # Wert enthalten (nach Clamping in set_max_charge_current), nicht den
                    # ungeclamppten Rampenwert. Sonst driftet die Hysterese bei niedrigen
                    # Stroemen (z.B. 5 A -> clamped auf 3 A) und erzeugt falsche Anzeigen.
                    self._last_written_ramped_a = self.state.charge_current_setpoint
                    write_performed = True
            else:
                self.state.charge_current_setpoint = self._last_written_ramped_a

        self.state.charge_mode              = mode
        self.state.charge_reason            = reason
        self.state.planned_charge_schedule  = self.build_schedule()

        # Persistenter Zustand alle 30 Minuten speichern
        if time.monotonic() - self._last_persistent_save > 1800:
            self._save_persistent()

        if self.cfg["logging"].get("log_decisions", True):
            a = self.state.charge_current_setpoint
            log_suffix = " [KEIN WRITE]" if (target_a >= 0 and not write_performed) else ""
            self.logger.info(f"[{mode.upper()}] {a:.0f}A | {reason[:120]}{log_suffix}")
