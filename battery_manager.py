#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# pylint: disable=logging-fstring-interpolation,broad-exception-caught
# pylint: disable=too-many-branches,too-many-statements,too-many-locals
# pylint: disable=protected-access
"""
=============================================================
Prognosebasiertes Laden - Batterielebensdauer optimieren
LFP Akku | Victron Multiplus II + Cerbo GX
=============================================================
Version: siehe version.py (Modbus TCP, kein MQTT)

Kommunikation:
- Lesen:     Modbus TCP -> Cerbo GX (Port 502, Unit-ID 100)
- Schreiben: Modbus TCP -> DVCC MaxChargeCurrent (Reg. 2705)
- evcc:      HTTP REST API -> Konflikt-Koordination

Steuerlogik:
- Verzögertes Laden am Morgen (PV-Prognose abwarten)
- SOC-Fenster bevorzugt 20-80 % (LFP-schonend)
- Alle N Tage Vollladung auf 98 % (Zellbalancing)
- evcc-Priorität: bei Schnellladen Auto -> eigene Steuerung pausieren
- evcc MinSoc-Sperre (Reg 2901) wird im Ladeplan berücksichtigt

Dateistruktur (ab v3.0.10.6):
- battery_manager.py  : Entry Point, Config-Validierung, Hauptschleife
- version.py          : Versionsstring (einzige Stelle)
- controller.py       : Ladelogik, ChargeController, EnergyAccumulator
- forecast.py         : VRM / Open-Meteo / Solcast / Dummy Prognose
- modbus_victron.py   : Modbus-TCP, Register-Mapping, DVCC-Schreiben
- evcc.py             : evcc REST-Polling, Reg-2901-Überwachung
- logging_setup.py    : DeduplicatingFilter, setup_logging
- models.py           : Dataclasses (SystemState, HourlyForecast, ...)
- dashboard.py        : HTML-Template, Flask-Server, Heartbeat-Thread
- CHANGELOG.md        : Versionshistorie

=============================================================
Victron Modbus-TCP Register (Cerbo GX, Unit-ID 100):
  843  /Soc                Battery SOC            raw ÷ 10  -> %
  840  /Voltage            Batteriespannung        raw ÷ 100 -> V
  841  /Current            Batteriestrom           raw ÷ 10  -> A  (signed)
  850  /Dc/Pv/Power        PV-Gesamtleistung       raw       -> W
  817  /Ac/Consumption L1  Verbrauch Phase 1       raw       -> W  (signed)
  818  /Ac/Consumption L2  Verbrauch Phase 2       raw       -> W  (signed)
  819  /Ac/Consumption L3  Verbrauch Phase 3       raw       -> W  (signed)
  820  /Ac/Grid L1         Netzbezug Phase 1       raw       -> W  (signed, + = Bezug)
  821  /Ac/Grid L2         Netzbezug Phase 2       raw       -> W
  822  /Ac/Grid L3         Netzbezug Phase 3       raw       -> W

  Schreiben (DVCC):
  2705 MaxChargeCurrent    Maximaler Ladestrom     raw       -> A
       Wert 0 = kein Laden, 50 = Maximalstrom

  ESS-Status (read-only):
  2900 BatteryLife State   ESS Zustand             raw       -> Enum
       Relevant fuer "Optimiert ohne BatteryLife" (Standard LFP):
       10=Self-consumption (SOC >= MinSOC) -> normaler Betrieb
       11=Self-consumption (SOC <  MinSOC) -> Entladen gesperrt!
       12=Recharge (SOC >5% unter MinSOC) -> Zwangsladung aus Netz
       Weitere States (nur bei "Optimiert mit BatteryLife"):
       2=Self-consumption  3=Self-consumption (SOC>85%)
       4=Self-consumption (SOC=100%)
       5=SOC below dynamic limit -> Entladen gesperrt (BatteryLife)
       6=SOC>24h unter Limit -> Laden mit 5A
       7=Multi/Quattro sustain
  2901 ESS MinimumSocLimit  Konfigurierter SOC-Mindestwert  raw / 10 -> %
       Wird von evcc temporaer angehoben beim Schnellladen.
       Unser battery_manager liest diesen Wert (EvccMonitor).
  2903 ESS Active SoC Limit  Nur relevant bei "Optimiert mit BatteryLife"
       Im Modus "Optimiert ohne BatteryLife" wird dieser Wert von
       Victron ignoriert -> nicht einlesen.

  Hinweis: Register koennen je nach Firmware-Version leicht abweichen.
  Pruefen mit:  mosquitto_sub oder Victron Modbus-TCP Register-Liste
  (https://github.com/victronenergy/venus-modbus-tcp-specification)
=============================================================
"""

import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from version import VERSION
from models import SystemState
from logging_setup import setup_logging
from modbus_victron import VictronModbus
from evcc import EvccMonitor
from forecast import ForecastManager
from controller import EnergyAccumulator, ChargeController
from dashboard import start_dashboard


def load_config(config_path: str = "config.yaml") -> dict:
    """Lädt die YAML-Konfiguration; sucht zuerst im übergebenen Pfad, dann neben der Skriptdatei."""
    path = Path(config_path)
    if not path.exists():
        path = Path(__file__).parent / "config.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─────────────────────────────────────────────
# Hauptprogramm
# ─────────────────────────────────────────────

def validate_config(cfg: dict, logger: logging.Logger) -> bool:
    """
    Prueft kritische Konfigurationswerte beim Start.
    Fail-Fast: lieber sofort mit klarer Fehlermeldung abbrechen
    als zur Laufzeit kryptische Fehler zu produzieren.
    """
    issues = []

    bat = cfg.get("battery", {})
    if not bat:
        issues.append("Abschnitt 'battery' fehlt komplett")
    else:
        if bat.get("capacity_kwh", 0) <= 0:
            issues.append("battery.capacity_kwh muss > 0 sein")
        if bat.get("max_charge_current", 0) <= 0:
            issues.append("battery.max_charge_current muss > 0 sein")
        if bat.get("min_charge_current", 0) < 0:
            issues.append("battery.min_charge_current muss >= 0 sein")
        min_soc = bat.get("min_soc", None)
        max_soc = bat.get("max_soc", None)
        if min_soc is None:
            issues.append("battery.min_soc fehlt")
        if max_soc is None:
            issues.append("battery.max_soc fehlt")
        if min_soc is not None and max_soc is not None and min_soc >= max_soc:
            issues.append(
                f"battery.min_soc ({min_soc}) muss kleiner als max_soc ({max_soc}) sein")

    modbus = cfg.get("modbus", {})
    if not modbus.get("host"):
        issues.append("modbus.host fehlt")

    charging = cfg.get("charging", {})
    if charging.get("control_interval_seconds", 60) < 5:
        issues.append("charging.control_interval_seconds muss >= 5 sein")

    loc = cfg.get("location", {})
    if "latitude" not in loc or "longitude" not in loc:
        issues.append("location.latitude / location.longitude fehlen")

    # v3.0.0: Optionale neue Felder validieren (falls vorhanden)
    cc = cfg.get("charging", {})
    if "solar_noon_offset_hours" in cc and cc["solar_noon_offset_hours"] < 0:
        issues.append("charging.solar_noon_offset_hours muss >= 0 sein")
    if "morning_delay_h" in cc and cc["morning_delay_h"] < 0:
        issues.append("charging.morning_delay_h muss >= 0 sein")
    if "reduced_charge_current_a" in cc:
        red_a = cc["reduced_charge_current_a"]
        max_a = bat.get("max_charge_current", 50)
        if red_a > max_a:
            issues.append(
                f"charging.reduced_charge_current_a ({red_a}) darf nicht groesser "
                f"als max_charge_current ({max_a}) sein")
        if red_a < 0:
            issues.append("charging.reduced_charge_current_a muss >= 0 sein")
    # v3.0.9.7: Dynamischer Strom im Optimal-Fenster
    if "optimal_window_min_current_a" in cc:
        opt_min = cc["optimal_window_min_current_a"]
        if opt_min < 0:
            issues.append("charging.optimal_window_min_current_a muss >= 0 sein")
        if "reduced_charge_current_a" in cc:
            red_a = cc["reduced_charge_current_a"]
            if opt_min > red_a:
                issues.append(
                    f"charging.optimal_window_min_current_a ({opt_min}) darf nicht groesser "
                    f"als reduced_charge_current_a ({red_a}) sein")

    # v3.0.9.26: required_a_smooth_window
    if "required_a_smooth_window" in cc:
        if cc["required_a_smooth_window"] < 1:
            issues.append("charging.required_a_smooth_window muss >= 1 sein")

    if issues:
        for issue in issues:
            logger.error(f"Config-Fehler: {issue}")
        return False
    return True


def _forecast_source(forecast: "ForecastManager") -> str:
    """Ermittelt welche Prognose-Quelle zuletzt verwendet wurde."""
    # Pruefe ob aktueller Cache von VRM stammt (nicht ob VRM jemals einen hatte)
    if forecast.vrm.enabled and forecast._cache is forecast.vrm._cache:
        return "vrm"
    provider = forecast.fc_cfg.get("provider", "open_meteo")
    if forecast._cache:
        return provider
    return "dummy"

def main():
    """Einstiegspunkt: Config laden, alle Subsysteme initialisieren, Hauptschleife starten."""
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    cfg    = load_config(config_path)
    logger, _, dedup_stream = setup_logging(cfg)

    logger.info("=" * 60)
    logger.info(f"Solar Batterie Manager v{VERSION}  (Modbus TCP) - Predictive Charging")
    logger.info(f"Konfiguration: {config_path}")
    logger.info("=" * 60)

    # Config-Validierung: lieber jetzt abbrechen als kryptische Laufzeitfehler
    if not validate_config(cfg, logger):
        logger.critical("Ungueltige Konfiguration – Programm wird beendet.")
        sys.exit(1)

    state  = SystemState()
    energy = EnergyAccumulator()

    # Initiale PV-Prognose
    forecast = ForecastManager(cfg, logger)
    logger.info("Lade initiale PV-Prognose...")
    try:
        fc = forecast.get_forecast(force=True)
        state.forecast_pv_today_kwh = round(sum(f.pv_kwh for f in fc), 2)
        now_h = datetime.now().hour
        state.forecast_pv_remaining_kwh = round(
            sum(f.pv_kwh for f in fc if f.hour >= now_h), 2)
        state.forecast_updated      = datetime.now().isoformat()
        state.forecast_source       = _forecast_source(forecast)
    except Exception as e:
        logger.error(f"Prognose-Fehler beim Start: {e}")

    # Victron Modbus
    victron = VictronModbus(cfg, state, logger)
    logger.info(
        f"Modbus TCP: {cfg['modbus']['host']}:{cfg['modbus'].get('port', 502)}")

    # Aktuellen Ist-Wert lesen (Startzustand)
    cur = victron.read_current_max_charge()
    if cur is not None:
        logger.info(f"Aktueller MaxChargeCurrent laut Cerbo: {cur} A")
        state.charge_current_setpoint = cur
        victron._last_written_a = cur   # Shadow-Variable Modbus-Layer vorbelegen

    # evcc Monitor
    evcc = EvccMonitor(cfg, state, victron, logger)
    if cfg.get("evcc", {}).get("enabled", False):
        logger.info(f"evcc Monitor: {cfg['evcc']['api_url']}")
    else:
        logger.info("evcc Monitor deaktiviert (evcc.enabled: false in config)")

    # Ladesteuerung
    controller = ChargeController(cfg, state, forecast, victron, evcc, logger)

    # Rampe und Schreib-Hysterese des Controllers mit Cerbo-Ist-Wert vorbelegen.
    # Ohne das wuerde _ramp_current bei 0 starten und sofort einen Write mit
    # einem zu niedrigen Rampenwert ausloesen (z.B. 5 A statt 50 A), was im
    # Dashboard als falscher Ladestrom erscheint und den Cerbo kurzzeitig
    # auf den Rampenwert setzt.
    if cur is not None:
        controller._ramp_current         = cur
        controller._last_written_ramped_a = cur

    # Dashboard
    if cfg.get("dashboard", {}).get("enabled", True):
        if dedup_stream is not None:
            start_dashboard(cfg, state, logger, dedup_stream, version=VERSION)
        else:
            logger.warning("dedup_stream=None: Dashboard ohne Deduplizierung gestartet")
            start_dashboard(cfg, state, logger, dedup_stream, version=VERSION)

    # Hauptschleife
    interval    = cfg["charging"].get("control_interval_seconds", 60)
    fc_interval = cfg["forecast"].get("update_interval_minutes", 60) * 60
    last_fc_ts  = 0.0

    logger.info(f"Steuerungsloop gestartet (Intervall: {interval}s)")

    try:
        while True:
            now_ts = time.monotonic()
            state.timestamp = datetime.now().isoformat()

            # 1. Messwerte per Modbus lesen
            victron.read_all()

            # 2. Tages-Energie aufsummieren (inkl. Batteriestrom-Integration)
            energy.update(state.pv_power_w, state.load_power_w, state.battery_power_w)
            state.pv_energy_today_kwh   = round(energy.pv_kwh,   3)
            state.load_energy_today_kwh = round(energy.load_kwh, 3)
            state.bat_energy_today_wh   = energy.bat_wh   # signed Wh, fuer _update_history

            # 3. evcc Status abfragen
            evcc.update()

            # 4. Prognose aktualisieren wenn faellig
            if now_ts - last_fc_ts > fc_interval:
                try:
                    # v3.0.9.4: Bei VRM immer force=True  -  VRM aktualisiert stündlich,
                    # besonders bei Wetterwechseln. Der Server cached selbst.
                    use_force = forecast.vrm.enabled
                    fc = forecast.get_forecast(force=use_force)
                    state.forecast_pv_today_kwh = round(sum(f.pv_kwh for f in fc), 2)
                    # NEU: Rest-Prognoes berechnen (nicht nur Tagesgesamt)
                    now_h = datetime.now().hour
                    state.forecast_pv_remaining_kwh = round(
                        sum(f.pv_kwh for f in fc if f.hour >= now_h), 2)
                    state.forecast_updated      = datetime.now().isoformat()
                    last_fc_ts = now_ts
                except Exception as e:
                    logger.error(f"Prognose-Update: {e}")

            # 5. Regelzyklus ausfuehren
            controller.run_cycle()

            time.sleep(interval)

    except KeyboardInterrupt:
        logger.info("Beendet durch Nutzer (Ctrl+C)")
        victron.set_max_charge_current(0)
    except Exception as e:
        logger.critical(f"Kritischer Fehler: {e}", exc_info=True)
        victron.set_max_charge_current(0)
        raise


if __name__ == "__main__":
    main()
