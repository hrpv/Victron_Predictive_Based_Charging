#!/usr/bin/env python3
"""
=============================================================
Prognosebasiertes Laden - Batterielebensdauer optimieren
LFP Akku | Victron Multiplus II + Cerbo GX
=============================================================
Version: 2.0.6  (Modbus TCP, kein MQTT)

Kommunikation:
- Lesen:     Modbus TCP → Cerbo GX (Port 502, Unit-ID 100)
- Schreiben: Modbus TCP → DVCC MaxChargeCurrent (Reg. 2705)
- evcc:      HTTP REST API → Konflikt-Koordination

Steuerlogik:
- Verzögertes Laden am Morgen (PV-Prognose abwarten)
- SOC-Fenster bevorzugt 20-80 % (LFP-schonend)
- Alle N Tage Vollladung auf 98 % (Zellbalancing)
- evcc-Priorität: bei Schnellladen Auto → eigene Steuerung pausieren

=============================================================
Victron Modbus-TCP Register (Cerbo GX, Unit-ID 100):
  843  /Soc                Battery SOC            raw ÷ 10  → %
  840  /Voltage            Batteriespannung        raw ÷ 100 → V
  841  /Current            Batteriestrom           raw ÷ 10  → A  (signed)
  850  /Dc/Pv/Power        PV-Gesamtleistung       raw       → W
  817  /Ac/Consumption L1  Verbrauch Phase 1       raw       → W  (signed)
  818  /Ac/Consumption L2  Verbrauch Phase 2       raw       → W  (signed)
  819  /Ac/Consumption L3  Verbrauch Phase 3       raw       → W  (signed)
  820  /Ac/Grid L1         Netzbezug Phase 1       raw       → W  (signed, + = Bezug)
  821  /Ac/Grid L2         Netzbezug Phase 2       raw       → W
  822  /Ac/Grid L3         Netzbezug Phase 3       raw       → W

  Schreiben (DVCC):
  2705 MaxChargeCurrent    Maximaler Ladestrom     raw       → A
       Wert 0 = kein Laden, 50 = Maximalstrom

  Hinweis: Register koennen je nach Firmware-Version leicht abweichen.
  Pruefen mit:  mosquitto_sub oder Victron Modbus-TCP Register-Liste
  (https://github.com/victronenergy/venus-modbus-tcp-specification)
=============================================================
"""

import json
import logging
import logging.handlers
import math
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional
import threading

import requests
import yaml

# pymodbus >= 3.0  (empfohlen)
try:
    from pymodbus.client import ModbusTcpClient
    from pymodbus.exceptions import ModbusException
except ImportError:
    # pymodbus 2.x Fallback
    from pymodbus.client.sync import ModbusTcpClient
    from pymodbus.exceptions import ModbusException


# ─────────────────────────────────────────────
# Datenstrukturen
# ─────────────────────────────────────────────

@dataclass
class SystemState:
    """Aktueller Systemzustand - wird vom Dashboard per JSON gelesen."""
    timestamp: str = ""
    soc: float = 50.0
    pv_power_w: float = 0.0
    pv_energy_today_kwh: float = 0.0        # intern aufsummiert
    load_power_w: float = 0.0
    load_energy_today_kwh: float = 0.0      # intern aufsummiert (EnergyAccumulator)
    grid_power_w: float = 0.0              # + = Bezug, - = Einspeisung
    battery_voltage: float = 0.0
    battery_current: float = 0.0           # + = laden, - = entladen
    battery_power_w: float = 0.0            # int16, direkt W, + = laden, - = entladen
    charge_current_setpoint: float = 0.0   # zuletzt geschriebener Wert [A]
    charge_mode: str = "idle"              # idle|charging|full_charge
    charge_reason: str = ""
    last_full_charge_date: str = ""
    days_since_full_charge: int = 0
    forecast_pv_today_kwh: float = 0.0
    forecast_pv_remaining_kwh: float = 0.0
    forecast_consumption_night_kwh: float = 0.0
    planned_charge_schedule: list = field(default_factory=list)
    history_buffer: list = field(default_factory=list)  # [HourlyHistory, ...]
    target_soc: float = 80.0
    modbus_connected: bool = False
    forecast_updated: str = ""
    forecast_source: str = ""        # vrm | solcast | open_meteo | dummy
    evcc_active: bool = False               # Auto wird gerade geladen (Info)
    evcc_discharge_locked: bool = False     # evcc hat Reg 2901 erhoeht → kein Entladen
    evcc_mode: str = ""                     # "off"|"now"|"minpv"|"pv"
    evcc_charge_power_w: float = 0.0        # Wallbox-Ladeleistung [W]
    evcc_min_soc: float = 0.0              # Aktueller Wert in Reg 2901 [%]


@dataclass
class HourlyForecast:
    hour: int
    pv_kwh: float
    consumption_kwh: float
    net_kwh: float                          # positiv = Ueberschuss


# ─────────────────────────────────────────────
# Konfiguration
# ─────────────────────────────────────────────



@dataclass
class HourlyHistory:
    """Speichert den tatsaechlichen Zustand einer vergangenen Stunde.

    WICHTIG: Diese Dataclass wird in build_schedule() per Punkt-Notation
    zugegriffen (hist.date_iso, hist.hour, etc.), NICHT per Dictionary!
    """
    date_iso: str          # YYYY-MM-DD
    hour: int
    pv_kwh: float          # Stuendliche PV-Erzeugung (nicht kumulativ!)
    consumption_kwh: float # Stuendlicher Verbrauch (nicht kumulativ!)
    surplus_kwh: float
    action: str            # idle|charging|full_charge|discharging
    charge_current_a: float
    soc_start: float       # SOC zu Stundenbeginn
    soc_end: float         # SOC zu Stundenende (letzter Messwert)
    is_actual: bool = True
    # Interne Felder fuer Differenzberechnung (nicht im Dashboard sichtbar)
    _raw_pv_total: float = 0.0      # Tageskumulativ PV (intern)
    _raw_cons_total: float = 0.0    # Tageskumulativ Verbrauch (intern)
    # NEU: Kumulativwerte zu Stundenbeginn (fuer korrekte Stundensumme)
    _hour_start_pv_total: float = 0.0
    _hour_start_cons_total: float = 0.0

def load_config(config_path: str = "config.yaml") -> dict:
    path = Path(config_path)
    if not path.exists():
        path = Path(__file__).parent / "config.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

def setup_logging(cfg: dict) -> logging.Logger:
    log_cfg = cfg.get("logging", {})
    log_file = log_cfg.get("file", "battery_manager.log")
    log_level = getattr(logging, log_cfg.get("level", "INFO"))
    max_bytes = log_cfg.get("max_size_mb", 10) * 1024 * 1024
    backup = log_cfg.get("backup_count", 3)

    logger = logging.getLogger("BatteryManager")
    if logger.handlers:
        return logger
    logger.setLevel(log_level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    import os
    is_systemd = bool(os.environ.get("INVOCATION_ID"))

    if is_systemd:
        # Systemd-Journal: stdout/stderr werden von journalctl erfasst.
        # FileHandler nur wenn explizit gewuenscht (nicht 'journal' oder leer).
        if log_file and log_file.lower() not in ("", "null", "/dev/null", "journal"):
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=max_bytes, backupCount=backup)
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        # StreamHandler fuer journalctl (StandardOutput=journal im Service)
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)
    else:
        # Manueller Start: File + Console
        if log_file:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=max_bytes, backupCount=backup)
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    logger.propagate = False
    return logger


# ─────────────────────────────────────────────
# Victron Modbus-TCP Interface
# ─────────────────────────────────────────────

class VictronModbus:
    """
    Kommuniziert mit dem Cerbo GX per Modbus TCP.

    Alle Verbindungen sind kurzlebig (connect -> lesen/schreiben -> close),
    damit kein dauerhafter Lock entsteht und evcc ungestoert parallel zugreifen kann.
    Schreiben erfolgt nur wenn sich der Sollwert um mindestens 1 A aendert.
    """

    # (Register-Adresse, Skalierungsfaktor, vorzeichenbehaftet)
    # Victron Register-IDs (0-basiert, wie pymodbus sie verwendet).
    # mbpoll zeigt +1 Offset (1-basiert) – hier stehen die echten IDs.
    REGISTERS = {
        "soc":      (843,  1.0,  False),   # % direkt          (mbpoll 844)
        "voltage":  (840,  0.1,  False),   # uint16 / 10 -> V  (mbpoll 841)
        "current":  (841,  0.1,  True),    # int16  / 10 -> A  (mbpoll 842)
        "batt_power":(842,  1.0,  True),   # int16, direkt W   (mbpoll 843)
        # PV-Wechselrichter AC-gekoppelt L1/L2/L3 (Victron 811/812/813 = mbpoll 812/813/814)
        "pv_l1":    (811,  1.0,  False),   # W
        "pv_l2":    (812,  1.0,  False),   # W
        "pv_l3":    (813,  1.0,  False),   # W
        # AC-Lasten L1/L2/L3 (Victron 817/818/819 = mbpoll 818/819/820)
        "load_l1":  (817,  1.0,  False),   # W
        "load_l2":  (818,  1.0,  False),   # W
        "load_l3":  (819,  1.0,  False),   # W
        # Netz L1/L2/L3 signed (Victron 820/821/822 = mbpoll 821/822/823)
        "grid_l1":  (820,  1.0,  True),    # W signed, + = Bezug, - = Einspeisung
        "grid_l2":  (821,  1.0,  True),
        "grid_l3":  (822,  1.0,  True),

    }
    REG_MAX_CHARGE = 2705   # DVCC MaxChargeCurrent [A]
    UNIT_ID = 100           # Cerbo GX Modbus Unit-ID

    def __init__(self, cfg: dict, state: SystemState, logger: logging.Logger):
        self.cfg = cfg
        self.state = state
        self.logger = logger
        mb = cfg["modbus"]
        self.host = mb["host"]
        self.port = mb.get("port", 502)
        self.timeout = mb.get("timeout_seconds", 5)
        self._last_written_a: Optional[float] = None

    def _new_client(self) -> ModbusTcpClient:
        return ModbusTcpClient(host=self.host, port=self.port, timeout=self.timeout)

    def _read_reg(self, client: ModbusTcpClient, addr: int,
                  scale: float, signed: bool) -> Optional[float]:
        try:
            r = client.read_holding_registers(addr, count=1, device_id=self.UNIT_ID)
            if r.isError():
                self.logger.debug(f"Modbus Lesefehler Reg {addr}: {r}")
                return None
            raw = r.registers[0]
            if signed and raw > 32767:
                raw -= 65536
            return raw * scale
        except Exception as e:
            self.logger.debug(f"Modbus Exception Reg {addr}: {e}")
            return None

    def read_all(self) -> bool:
        """Liest alle Messwerte vom Cerbo GX."""
        client = self._new_client()
        try:
            if not client.connect():
                self.logger.warning(
                    f"Modbus: keine Verbindung zu {self.host}:{self.port}")
                self.state.modbus_connected = False
                return False

            self.state.modbus_connected = True

            def r(name):
                addr, scale, signed = self.REGISTERS[name]
                return self._read_reg(client, addr, scale, signed)

            soc     = r("soc")
            voltage = r("voltage")
            current = r("current")
            batt_pw = r("batt_power")
            pv_l1   = r("pv_l1") or 0.0
            pv_l2   = r("pv_l2") or 0.0
            pv_l3   = r("pv_l3") or 0.0
            l1      = r("load_l1") or 0.0
            l2      = r("load_l2") or 0.0
            l3      = r("load_l3") or 0.0
            g1      = r("grid_l1") or 0.0
            g2      = r("grid_l2") or 0.0
            g3      = r("grid_l3") or 0.0

            if soc     is not None: self.state.soc             = round(soc, 1)
            if voltage is not None: self.state.battery_voltage  = round(voltage, 2)
            if current is not None: self.state.battery_current  = round(current, 1)
            if batt_pw is not None: self.state.battery_power_w  = round(batt_pw, 0)
            self.state.pv_power_w = round(pv_l1 + pv_l2 + pv_l3, 0)

            self.state.load_power_w = round(l1 + l2 + l3, 0)
            self.state.grid_power_w = round(g1 + g2 + g3, 0)
            self.state.timestamp    = datetime.now().isoformat()
            return True

        except Exception as e:
            self.logger.error(f"Modbus read_all: {e}")
            self.state.modbus_connected = False
            return False
        finally:
            client.close()

    def set_max_charge_current(self, current_a: float) -> bool:
        """
        Schreibt DVCC MaxChargeCurrent (Register 2705).

        Schicht-1-Schutz (Shadow-Variable): Identischer Wert wird niemals
        ein zweites Mal auf den Bus geschrieben – unabhaengig davon was der
        Aufrufer uebergibt. _last_written_a wird beim Start via
        read_current_max_charge() vorbelegt, damit auch der allererste
        Zyklus keinen unnoetigen Write ausloest.

        Schicht-2-Schutz liegt beim ChargeController (_last_written_ramped_a).
        Beide Schichten sind unabhaengig voneinander und sichern sich gegenseitig ab.
        """
        bat = self.cfg["battery"]
        current_a = max(float(bat["min_charge_current"]),
                        min(float(bat["max_charge_current"]), current_a))
        current_a = round(current_a)

        # Schicht-1: Shadow-Variable – identischer Wert → kein Write
        if self._last_written_a is not None and self._last_written_a == current_a:
            self.state.charge_current_setpoint = current_a
            return True   # aus Sicht des Aufrufers erfolgreich (Wert stimmt bereits)

        client = self._new_client()
        try:
            if not client.connect():
                self.logger.warning("Modbus: Schreiben nicht moeglich")
                return False

            result = client.write_register(
                self.REG_MAX_CHARGE, int(current_a), device_id=self.UNIT_ID)

            if result.isError():
                self.logger.error(
                    f"Modbus Schreibfehler Reg {self.REG_MAX_CHARGE}: {result}")
                return False

            self._last_written_a = current_a
            self.state.charge_current_setpoint = current_a
            self.logger.info(f"Modbus WRITE MaxChargeCurrent = {current_a} A")
            return True

        except Exception as e:
            self.logger.error(f"Modbus set_max_charge_current: {e}")
            return False
        finally:
            client.close()

    def read_register(self, addr: int, signed: bool = False,
                      scale: float = 1.0) -> Optional[float]:
        """Liest ein einzelnes Register (fuer evcc MinSoc-Abfrage o.ae.)."""
        client = self._new_client()
        try:
            if not client.connect():
                return None
            r = client.read_holding_registers(addr, count=1, device_id=self.UNIT_ID)
            if r.isError():
                return None
            raw = r.registers[0]
            if signed and raw > 32767:
                raw -= 65536
            return raw * scale
        except Exception:
            return None
        finally:
            client.close()

    def read_current_max_charge(self) -> Optional[float]:
        """Liest den aktuell am Cerbo gesetzten MaxChargeCurrent zurueck."""
        client = self._new_client()
        try:
            if not client.connect():
                return None
            r = client.read_holding_registers(
                self.REG_MAX_CHARGE, count=1, device_id=self.UNIT_ID)
            if r.isError():
                return None
            return float(r.registers[0])
        except Exception:
            return None
        finally:
            client.close()


# ─────────────────────────────────────────────
# evcc Koordination
# ─────────────────────────────────────────────

class EvccMonitor:
    """
    Ueberwacht evcc per REST API.

    Korrekte evcc-Registerbelegung (Victron Modbus TCP):
      Register 2901  ESS MinSoc  [%]
        - Normalzustand:   10–20 %  (konfigurierter Min-SOC)
        - Schnellladen:    ≈ aktueller SOC  (verhindert Akkuentladung)
        - Nach Schnellladen: wieder 10–20 %

      Register 2705  DVCC MaxChargeCurrent  [A]
        - evcc schreibt dieses Register NICHT
        - Wird ausschliesslich von diesem Skript gesteuert
        -> Kein Schreibkonflikt auf Reg 2705

    Was wir aus evcc ableiten:
      - evcc_active:        Auto wird gerade geladen (info)
      - evcc_mode:          "off"|"now"|"minpv"|"pv"
      - evcc_charge_power:  Ladeleistung Wallbox [W]
      - evcc_discharge_locked: Reg 2901 > normaler MinSoc
                                → evcc sperrt Akkuentladung
                                → wir duerfen laden, aber SOC-Ziel
                                  nicht unter evcc-MinSoc senken

    Einfluss auf Ladesteuerung:
      - evcc_discharge_locked = True:
          Unser effektiver SOC-Mindestwert = evcc_min_soc (aus Reg 2901)
          statt dem konfigurierten battery.min_soc.
          Laden laeuft normal weiter – kein Register-Konflikt.
    """

    REG_MIN_SOC = 2901      # ESS MinSoc [%], gelesen via Modbus
    NORMAL_MIN_SOC_MAX = 25 # Alles darueber gilt als "evcc hat gesperrt"

    def __init__(self, cfg: dict, state: SystemState,
                 victron: "VictronModbus", logger: logging.Logger):
        self.cfg     = cfg
        self.state   = state
        self.victron = victron
        self.logger  = logger
        evcc_cfg = cfg.get("evcc", {})
        self.enabled       = evcc_cfg.get("enabled", False)
        self.url           = evcc_cfg.get("api_url", "http://localhost:7070/api/state")
        self.timeout       = evcc_cfg.get("timeout_seconds", 5)
        self._poll_interval = evcc_cfg.get("poll_interval_seconds", 30)
        self._last_check: float = 0.0
        # Aktuell vom Modbus gelesener MinSoc-Wert
        self.evcc_min_soc: float = 0.0

    def update(self) -> None:
        """Liest evcc-Status (REST) und MinSoc-Register (Modbus)."""
        if not self.enabled:
            self.state.evcc_active          = False
            self.state.evcc_discharge_locked = False
            return

        now = time.monotonic()
        if now - self._last_check < self._poll_interval:
            return
        self._last_check = now

        # ── MinSoc aus Modbus Register 2901 lesen ─────────────
        min_soc_reg = self.victron.read_register(self.REG_MIN_SOC)
        if min_soc_reg is not None:
            self.evcc_min_soc = float(min_soc_reg)
            # evcc sperrt Entladung wenn MinSoc deutlich ueber Normalwert liegt
            self.state.evcc_discharge_locked = (
                self.evcc_min_soc > self.NORMAL_MIN_SOC_MAX)
            if self.state.evcc_discharge_locked:
                self.logger.debug(
                    f"evcc hat Entladung gesperrt: MinSoc-Reg={self.evcc_min_soc:.0f}%")

        # ── evcc REST API fuer Zusatzinfos ────────────────────
        try:
            resp = requests.get(self.url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()

            loadpoints = (data.get("result", {}).get("loadpoints")
                          or data.get("loadpoints", []))

            evcc_power = 0.0
            evcc_mode  = "off"
            charging   = False

            for lp in loadpoints:
                power = float(lp.get("chargePower", 0) or 0)
                if lp.get("charging", False) and power > 100:
                    charging    = True
                    evcc_power += power
                    evcc_mode   = lp.get("mode", "off")

            self.state.evcc_active         = charging
            self.state.evcc_mode           = evcc_mode
            self.state.evcc_charge_power_w = round(evcc_power, 0)

        except requests.exceptions.ConnectionError:
            pass   # evcc laeuft nicht -> kein Problem
        except Exception as e:
            self.logger.warning(f"evcc REST Fehler: {e}")



# ─────────────────────────────────────────────
# VRM API Prognose
# ─────────────────────────────────────────────

class VrmForecastManager:
    """
    Holt PV- und Verbrauchsprognose direkt von der Victron VRM API.

    Vorteile gegenueber Open-Meteo:
    - Solcast-Satellitendaten + ML-Modell auf deiner Anlagenhistorie
    - Anlagenspezifisch kalibriert (kennt deine realen Ertragsdaten)
    - Zusaetzlich: Verbrauchsprognose basierend auf historischem Muster
    - Rollierende Tagesprognose: Restwert + bereits erzeugte Energie

    Voraussetzungen:
    - Anlage muss in VRM registriert sein (mind. 30 Tage Daten)
    - VRM Access Token (VRM Portal → Einstellungen → Integrationen → Access Tokens)
    - Installation ID (Zahl in der VRM-URL: vrm.victronenergy.com/installation/XXXXX)

    Fallback: bei Fehler wird Open-Meteo verwendet.
    """

    BASE_URL = "https://vrmapi.victronenergy.com/v2"

    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg    = cfg
        self.logger = logger
        vrm = cfg.get("vrm", {})
        self.enabled      = vrm.get("enabled", False)
        self.token        = vrm.get("access_token", "")
        self.install_id   = vrm.get("installation_id", "")
        self.timeout      = vrm.get("timeout_seconds", 10)
        self._cache: Optional[list] = None
        self._cache_ts: float = 0.0
        self._consumption_night_kwh: float = 0.0

    def _headers(self) -> dict:
        return {"x-authorization": f"Token {self.token}",
                "Content-Type": "application/json"}

    def _sundown_unix(self) -> int:
        """Sonnenuntergang heute: 21 Uhr Ortszeit als Unix-Timestamp."""
        now = datetime.now()
        sundown = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if sundown < now:
            sundown = sundown + timedelta(days=1)
        return int(sundown.timestamp())   # mktime-Aequivalent, Lokalzeit korrekt

    def fetch(self, force: bool = False) -> Optional[list]:
        """
        Gibt stundliche HourlyForecast-Liste zurueck oder None bei Fehler.
        Strategie: Ganzen heutigen Tag abfragen (00:00 bis 23:59),
        damit alle Stunden verfuegbar sind und Gesamttag korrekt summiert wird.
        """
        if not self.enabled or not self.token or not self.install_id:
            return None

        interval_s = self.cfg.get("forecast", {}).get(
            "update_interval_minutes", 60) * 60
        # Cache bei Tageswechsel immer verwerfen (neue VRM-Prognose verfügbar)
        cache_day = getattr(self, "_cache_day", None)
        today = datetime.now().date()
        if cache_day != today:
            force = True
            self._cache_day = today
        if not force and self._cache and (
                time.monotonic() - self._cache_ts) < interval_s:
            return self._cache

        try:
            now   = datetime.now()
            # Start: Mitternacht heute Lokalzeit (timestamp() berücksichtigt Zeitzone)
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            # End: 23:59 heute Lokalzeit
            end   = now.replace(hour=23, minute=59, second=59, microsecond=0)
            start_unix = int(start.timestamp())   # korrekte Lokalzeit → Unix
            end_unix   = int(end.timestamp())
            avg_h = self.cfg["charging"].get(
                "avg_daily_consumption_kwh", 8.0) / 24

            url = (f"{self.BASE_URL}/installations/{self.install_id}/stats"
                   f"?type=forecast"
                   f"&start={start_unix}"
                   f"&end={end_unix}"
                   f"&interval=hours")

            self.logger.info(f"VRM API Request: {url}")
            self.logger.info(
                f"VRM Zeitbereich lokal: {start} bis {end}  |  "
                f"UTC: {datetime.utcfromtimestamp(start_unix)} bis "
                f"{datetime.utcfromtimestamp(end_unix)}"
            )

            resp = requests.get(url, headers=self._headers(),
                                timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()

            records = data.get("records", {})
            pv_raw   = records.get("solar_yield_forecast", [])
            cons_raw = records.get("vrm_consumption_fc",   [])

            self.logger.info(
                f"VRM API Eintraege: PV={len(pv_raw)}, Cons={len(cons_raw)}"
            )
            self.logger.debug(f"VRM API raw (trunc): {str(data)[:500]}")

            if not pv_raw:
                self.logger.warning("VRM API: keine PV-Prognosedaten")
                return None

            # Verbrauchsprognose fuer die Nacht berechnen
            cons_total_wh = data.get("totals", {}).get("vrm_consumption_fc", 0)
            if cons_total_wh:
                ns  = self.cfg["charging"].get("night_start_hour", 21)
                ne  = self.cfg["charging"].get("night_end_hour",   6)
                night_hours = (24 - ns) + ne
                self._consumption_night_kwh = (
                    cons_total_wh / 1000.0 / 24.0 * night_hours)
            else:
                self._consumption_night_kwh = 0.0

            # KORREKTUR: Aggregation pro Stunde (mehrere Eintraege werden summiert)
            pv_by_hour: dict = {}
            cons_by_hour: dict = {}

            for entry in pv_raw:
                if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                    continue
                ts_raw, wh = entry[0], entry[1]
                if wh is None:
                    continue
                ts_sec = ts_raw / 1000 if ts_raw > 1e10 else ts_raw
                h = datetime.fromtimestamp(ts_sec).hour
                pv_by_hour[h] = pv_by_hour.get(h, 0.0) + float(wh)

            for entry in cons_raw:
                if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                    continue
                ts_raw, wh = entry[0], entry[1]
                if wh is None:
                    continue
                ts_sec = ts_raw / 1000 if ts_raw > 1e10 else ts_raw
                h = datetime.fromtimestamp(ts_sec).hour
                cons_by_hour[h] = cons_by_hour.get(h, 0.0) + float(wh)

            # PV-Prognose in HourlyForecast umwandeln
            out = []
            total_pv_kwh = 0.0
            for h in sorted(pv_by_hour.keys()):
                wh = pv_by_hour[h]
                pv_kwh = float(wh) / 1000.0
                c_kwh  = cons_by_hour.get(h, avg_h * 1000) / 1000.0 if h in cons_by_hour else avg_h
                out.append(HourlyForecast(
                    hour=h,
                    pv_kwh=round(pv_kwh, 3),
                    consumption_kwh=round(c_kwh, 3),
                    net_kwh=round(pv_kwh - c_kwh, 3)
                ))
                total_pv_kwh += pv_kwh

            if not out:
                return None

            # DEBUG: Stunden 10-12 fuer Vergleich mit Portal loggen
            for hf in out:
                if 10 <= hf.hour <= 12:
                    self.logger.info(
                        f"VRM Stunde {hf.hour:02d}:00 -> PV={hf.pv_kwh:.3f} kWh, "
                        f"Cons={hf.consumption_kwh:.3f} kWh"
                    )

            self._cache    = out
            self._cache_ts = time.monotonic()
            self.logger.info(
                f"VRM-Prognose: {total_pv_kwh:.2f} kWh PV heute"
                + (f", Nachtverbrauch ~{self._consumption_night_kwh:.1f} kWh"
                   if self._consumption_night_kwh else ""))
            return out

        except requests.exceptions.HTTPError as e:
            self.logger.warning(f"VRM API HTTP-Fehler: {e} – Fallback Open-Meteo")
        except requests.exceptions.ConnectionError:
            self.logger.warning("VRM API nicht erreichbar – Fallback Open-Meteo")
        except Exception as e:
            self.logger.warning(f"VRM API Fehler: {e} – Fallback Open-Meteo")
        return None

    def night_consumption_kwh(self) -> Optional[float]:
        """Gibt VRM-Verbrauchsprognose fuer die Nacht zurueck, oder None."""
        return self._consumption_night_kwh if self._consumption_night_kwh else None

# ─────────────────────────────────────────────
# PV-Prognose
# ─────────────────────────────────────────────

class ForecastManager:
    """
    Holt stundliche PV-Prognose.
    Prioritaet: VRM API (anlagenspezifisch) → Solcast → Open-Meteo → Dummy
    """

    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg = cfg
        self.logger = logger
        self.loc = cfg["location"]
        self.pv_cfg = cfg["pv"]
        self.fc_cfg = cfg["forecast"]
        self._cache: Optional[list] = None
        self._cache_ts: float = 0.0
        # VRM als primäre Prognose-Quelle
        self.vrm = VrmForecastManager(cfg, logger)

    def get_forecast(self, force: bool = False) -> list:
        interval_s = self.fc_cfg.get("update_interval_minutes", 60) * 60
        # Cache bei Tageswechsel immer verwerfen
        cache_day = getattr(self, "_cache_day", None)
        today = datetime.now().date()
        if cache_day != today:
            force = True
            self._cache_day = today
        if not force and self._cache and (time.monotonic() - self._cache_ts) < interval_s:
            return self._cache

        # 1. VRM API (beste Qualitaet: Solcast + Anlagenhistorie)
        vrm_fc = self.vrm.fetch(force=force)
        if vrm_fc:
            self._cache    = vrm_fc
            self._cache_ts = time.monotonic()
            return vrm_fc  # forecast_source wird in main() gesetzt

        # 2. Fallback: Solcast oder Open-Meteo
        try:
            provider = self.fc_cfg.get("provider", "open_meteo")
            if provider == "solcast":
                fc = self._fetch_solcast()
            else:
                fc = self._fetch_open_meteo()
            self._cache    = fc
            self._cache_ts = time.monotonic()
            self.logger.info(
                f"Prognose ({provider}): {sum(f.pv_kwh for f in fc):.2f} kWh heute")
            return fc
        except Exception as e:
            self.logger.error(f"Prognose-Fehler: {e}")
            return self._cache or self._dummy()

    def _fetch_open_meteo(self) -> list:
        lat = self.loc["latitude"]
        lon = self.loc["longitude"]
        tz  = self.loc.get("timezone", "Europe/Berlin")
        url = (f"https://api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lon}"
               f"&hourly=shortwave_radiation,cloud_cover"
               f"&timezone={tz}&forecast_days=2")
        data = requests.get(url, timeout=15).json()
        times      = data["hourly"]["time"]
        radiation  = data["hourly"].get("shortwave_radiation", [0]*len(times))
        cloud      = data["hourly"].get("cloud_cover",         [0]*len(times))
        peak_kw    = self.pv_cfg["peak_power_kwp"]
        eff        = self.pv_cfg.get("efficiency_factor", 0.82)
        avg_h      = self.cfg["charging"].get("avg_daily_consumption_kwh", 8.0) / 24
        today      = datetime.now().date()
        out = []
        for i, t_str in enumerate(times):
            t = datetime.fromisoformat(t_str)
            if t.date() != today:
                continue
            rad  = radiation[i] or 0
            cl   = (cloud[i] or 0) / 100.0
            pv   = max(0.0, (rad / 1000.0) * peak_kw * eff * (1 - cl * 0.3))
            out.append(HourlyForecast(hour=t.hour, pv_kwh=round(pv,3),
                                      consumption_kwh=round(avg_h,3),
                                      net_kwh=round(pv - avg_h,3)))
        return out or self._dummy()

    def _fetch_solcast(self) -> list:
        key = self.fc_cfg.get("solcast_api_key", "")
        rid = self.fc_cfg.get("solcast_resource_id", "")
        if not key or not rid:
            self.logger.warning("Solcast: API-Key fehlt -> Open-Meteo als Fallback")
            return self._fetch_open_meteo()
        url = (f"https://api.solcast.com.au/rooftop_sites/{rid}"
               f"/forecasts?format=json&hours=24")
        data = requests.get(url, headers={"Authorization": f"Bearer {key}"},
                            timeout=15).json()
        avg_h = self.cfg["charging"].get("avg_daily_consumption_kwh", 8.0) / 24
        out = []
        for p in data.get("forecasts", []):
            t  = datetime.fromisoformat(p["period_end"].replace("Z", "+00:00"))
            pv = float(p.get("pv_estimate", 0))
            out.append(HourlyForecast(hour=t.hour, pv_kwh=round(pv,3),
                                      consumption_kwh=round(avg_h,3),
                                      net_kwh=round(pv - avg_h,3)))
        return out or self._dummy()

    def _dummy(self) -> list:
        self.logger.warning("Verwende Dummy-PV-Prognose")
        peak_kw = self.pv_cfg["peak_power_kwp"]
        eff     = self.pv_cfg.get("efficiency_factor", 0.82)
        avg_h   = self.cfg["charging"].get("avg_daily_consumption_kwh", 8.0) / 24
        out = []
        for h in range(24):
            pv = 0.0
            if 6 <= h <= 20:
                x  = (h - 13) / 4.0
                pv = peak_kw * eff * math.exp(-x * x) * 0.7
            out.append(HourlyForecast(hour=h, pv_kwh=round(pv,3),
                                      consumption_kwh=round(avg_h,3),
                                      net_kwh=round(pv - avg_h,3)))
        return out

    def pv_remaining_kwh(self) -> float:
        now_h = datetime.now().hour
        return sum(f.pv_kwh for f in self.get_forecast() if f.hour >= now_h)

    def pv_total_kwh(self) -> float:
        return sum(f.pv_kwh for f in self.get_forecast())

    def night_consumption_kwh(self) -> float:
        """VRM-Verbrauchsprognose bevorzugt, sonst Durchschnitt."""
        vrm_val = self.vrm.night_consumption_kwh()
        if vrm_val:
            return vrm_val
        ns  = self.cfg["charging"].get("night_start_hour", 21)
        ne  = self.cfg["charging"].get("night_end_hour",   6)
        avg = self.cfg["charging"].get("avg_daily_consumption_kwh", 8.0) / 24
        return avg * ((24 - ns) + ne)


# ─────────────────────────────────────────────
# Energie-Tagesintegration
# ─────────────────────────────────────────────

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
        self._day: Optional[date] = None
        self.pv_kwh:   float = 0.0
        self.load_kwh: float = 0.0

    def update(self, pv_w: float, load_w: float):
        now_ts = time.monotonic()
        today  = date.today()
        if self._day and self._day != today:   # Tageswechsel
            self.pv_kwh = self.load_kwh = 0.0
        self._day = today
        if self._last_ts is not None:
            dt_s = now_ts - self._last_ts
            # Zu schnelle Updates ueberspringen – Messwert wird gemerkt,
            # Zeitstempel aber NICHT aktualisiert, damit das naechste
            # gueltiges Update das korrekte Intervall sieht.
            if dt_s < self.MIN_UPDATE_INTERVAL_S:
                self._last_pv = pv_w
                self._last_ld = load_w
                return
            dt_h = dt_s / 3600.0
            self.pv_kwh   += self._last_pv * dt_h / 1000.0
            self.load_kwh += self._last_ld * dt_h / 1000.0
        self._last_ts = now_ts
        self._last_pv = pv_w
        self._last_ld = load_w


# ─────────────────────────────────────────────
# Ladeentscheidungs-Engine
# ─────────────────────────────────────────────

class ChargeController:
    """
    Kernlogik: entscheidet Modus und Ladestrom.

    Prioritaeten (absteigend):
      1. evcc Schnellladen        -> Steuerung abgeben (nichts schreiben)
      2. Notfall SOC              -> Volllast sofort
      3. Vollladung faellig       -> auf max_soc laden (Balancing)
      4. Nacht                    -> kein Laden
      5. Morgen-Verzoegerung      -> warte auf PV wenn Prognose ausreicht
      6. PV-Ueberschuss           -> laden proportional zum Ueberschuss
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
        self._load_energy_base()
        # Hysterese: Mindestdauer einer Ladeentscheidung
        self._last_decision_ts: float = 0.0
        self._last_decision_result: tuple[float, str, str] = (0.0, "idle", "Initialisierung")
        self._min_charge_duration_s: float = self.cc.get("min_charge_duration_minutes", 10) * 60
        # Letzter geschriebener Wert (nach Rampe) fuer Schreib-Hysterese
        self._last_written_ramped_a: float = 0.0
        # Persistenter Zustand: nur alle 5 Minuten auf SD-Karte schreiben
        self._last_persistent_save: float = 0.0

    def _load_energy_base(self):
        """Laedt letzten bekannten Energie-Akkumulatorstand aus state.json."""
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text())
                saved_date = data.get("energy_date", "")
                today = date.today().isoformat()
                if saved_date == today:
                    # Selber Tag: Basis wiederherstellen
                    self._energy_base_pv   = float(data.get("energy_base_pv",   0.0))
                    self._energy_base_load = float(data.get("energy_base_load", 0.0))
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
            data = json.loads(self._state_file.read_text())
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
            import tempfile, os
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            # Aktuellen Gesamtenergie-Stand = Akkumulator + Basis aus letztem Neustart
            pv_total   = self.state.pv_energy_today_kwh   + self._energy_base_pv
            load_total = self.state.load_energy_today_kwh + self._energy_base_load
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
            }, indent=2)
            # Atomic write: erst in Temp-Datei, dann atomares rename().
            # Verhindert korrupte state.json bei Stromausfall waehrend des Schreibens.
            fd, tmp_path = tempfile.mkstemp(
                dir=self._state_file.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
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

    def _is_morning_window(self) -> bool:
        h = datetime.now().hour
        return (self.cc.get("morning_delay_start_hour", 6)
                <= h < self.cc.get("morning_delay_end_hour", 10))

    def _is_night(self) -> bool:
        h = datetime.now().hour
        return (h >= self.cc.get("night_start_hour", 21)
                or h < self.cc.get("night_end_hour", 6))

    def _soc_for_kwh(self, kwh: float) -> float:
        return (kwh / self.bat["capacity_kwh"]) * 100.0

    # --- Hauptentscheidung ---

    def decide(self) -> tuple[float, str, str]:
        """Gibt (Ladestrom_A, Modus, Begruendung) zurueck.
           Ladestrom -1 bedeutet: nicht schreiben (evcc hat Kontrolle)."""

        soc       = self.state.soc
        max_soc   = self.bat["max_soc"]
        target_n  = self.bat["target_soc_normal"]
        hyst      = self.cc.get("soc_hysteresis", 2)
        emergency = self.cc.get("emergency_charge_soc", 25)
        nom_v     = self.bat.get("voltage_nominal", 48.0)
        max_a     = self.bat["max_charge_current"]
        trickle_a = self.bat.get("trickle_current", 5)

        # ── Effektiver Min-SOC: evcc kann diesen angehoben haben ─
        # evcc schreibt beim Schnellladen Reg 2901 (MinSoc) auf ~aktuellen SOC
        # um Akkuentladung zu verhindern. Wir respektieren das als Untergrenze.
        # Unser MaxChargeCurrent (Reg 2705) bleibt davon voellig unberuehrt.
        effective_min_soc = self.bat["min_soc"]
        if self.state.evcc_discharge_locked:
            effective_min_soc = max(effective_min_soc,
                                    self.evcc.evcc_min_soc)
            self.logger.debug(
                f"evcc MinSoc-Sperre aktiv: effektiver Min-SOC = "
                f"{effective_min_soc:.0f}% (Reg 2901={self.evcc.evcc_min_soc:.0f}%)")

        # ── 2. Notfall ────────────────────────────────────────
        if soc <= emergency:
            return max_a, "charging", (
                f"NOTFALL: SOC {soc:.1f}% <= {emergency}% -> sofort laden")

        # ── 3. Vollladung faellig (Zellbalancing) ─────────────
        if self._needs_full_charge():
            if soc >= max_soc - hyst:
                self.state.last_full_charge_date = date.today().isoformat()
                self.state.days_since_full_charge = 0
                self._save_persistent()
                return 0, "idle", f"Vollladung abgeschlossen ({soc:.1f}%)"
            return max_a, "full_charge", (
                f"Vollladung faellig ({self.state.days_since_full_charge} Tage) "
                f"-> lade auf {max_soc}% (aktuell {soc:.1f}%)")

        # ── 4. Nacht -> kein Laden ────────────────────────────
        if self._is_night():
            return 0, "idle", f"Nacht: kein Laden (SOC {soc:.1f}%)"

        # ── Prognose-Daten ────────────────────────────────────
        pv_rem     = self.forecast.pv_remaining_kwh()
        pv_total   = self.forecast.pv_total_kwh()
        night_cons = self.forecast.night_consumption_kwh()

        # Projizierter Abend-SOC (aktueller SOC + restliche PV-Erzeugung)
        proj_eve = min(max_soc, soc + self._soc_for_kwh(pv_rem))

        # Dynamisches Ladeziel: erhoehen wenn PV nicht reicht
        if proj_eve >= target_n:
            dyn_target = target_n
        else:
            shortfall  = target_n - proj_eve
            dyn_target = min(target_n + shortfall * 0.5, max_soc)

        self.state.target_soc                    = round(dyn_target)
        self.state.forecast_pv_remaining_kwh     = round(pv_rem, 2)
        self.state.forecast_pv_today_kwh         = round(pv_total, 2)
        self.state.forecast_consumption_night_kwh = round(night_cons, 2)

        # Ziel bereits erreicht?
        if soc >= dyn_target - hyst:
            return 0, "idle", (
                f"Ziel {dyn_target:.0f}% erreicht (SOC {soc:.1f}%)")

        # ── 5. Morgen-Verzoegerung ────────────────────────────
        if self._is_morning_window():
            if proj_eve >= target_n:
                return 0, "idle", (
                    f"Morgen: PV-Prognose {pv_total:.1f} kWh reicht, "
                    f"Abend-SOC ~{proj_eve:.0f}% erwartet -> warte auf PV")
            if soc > target_n - 15:
                return 0, "idle", (
                    f"Morgen: SOC {soc:.1f}% noch ausreichend, "
                    f"warte auf PV ({pv_rem:.1f} kWh erwartet)")

        # ── 6. PV-Ueberschuss ─────────────────────────────────
        pv_w     = self.state.pv_power_w
        load_w   = self.state.load_power_w
        surplus_w = max(0.0, pv_w - load_w)

        if surplus_w > 200:
            surplus_a = surplus_w / nom_v
            charge_a  = min(surplus_a, max_a)
            return charge_a, "charging", (
                f"PV-Ueberschuss {surplus_w:.0f} W -> {charge_a:.0f} A "
                f"(SOC {soc:.1f}% -> Ziel {dyn_target:.0f}%)")

        # ── 7. Trickle ────────────────────────────────────────
        if soc < target_n - 10:
            return trickle_a, "trickle", (
                f"SOC {soc:.1f}% weit unter Ziel {dyn_target:.0f}%, "
                f"sanft laden {trickle_a} A "
                f"(PV {pv_w:.0f} W, Last {load_w:.0f} W)")

        # ── 8. Warten ─────────────────────────────────────────
        return 0, "idle", (
            f"Warte auf PV-Ueberschuss "
            f"(SOC {soc:.1f}%, PV {pv_w:.0f} W, Last {load_w:.0f} W)")

    def _ramp(self, target_a: float) -> float:
        """Sanftes Rampen des Ladestroms (+/- ramp_step A pro Zyklus)."""
        step = self.cc.get("current_ramp_step", 5)
        if target_a > self._ramp_current:
            self._ramp_current = min(self._ramp_current + step, target_a)
        elif target_a < self._ramp_current:
            self._ramp_current = max(self._ramp_current - step, target_a)
        return self._ramp_current

    def _simulate_hour(self, h: int, fc: HourlyForecast, soc_sim: float, 
                       needs_full: bool, pv_rem_total: float, night_cons: float,
                       is_forecast: bool = True) -> tuple[str, float, float]:
        """
        Simuliert EINE Stunde mit der exakten decide()-Logik.
        Gibt (action, current_a, new_soc_sim) zurueck.
        """
        cap = self.bat["capacity_kwh"]
        min_soc = self.bat["min_soc"]
        max_soc = self.bat["max_soc"]
        nom_v = self.bat.get("voltage_nominal", 48.0)
        max_a = self.bat["max_charge_current"]
        target_n = self.bat["target_soc_normal"]
        hyst = self.cc.get("soc_hysteresis", 2)
        morn_s = self.cc.get("morning_delay_start_hour", 6)
        morn_e = self.cc.get("morning_delay_end_hour", 10)

        action = "idle"
        current_a = 0.0

        # Notfall-SOC
        if soc_sim <= self.cc.get("emergency_charge_soc", 25):
            action = "charging"
            current_a = max_a
            soc_sim = min(max_soc, soc_sim + (current_a * nom_v / 1000 / cap) * 100)
            return action, current_a, soc_sim

        # Maximale Ladeenergie pro Stunde durch Strombegrenzung
        max_charge_kwh = max_a * nom_v / 1000.0  # z.B. 50A * 48V / 1000 = 2.4 kWh

        if needs_full and soc_sim < max_soc - hyst:
            action = "full_charge"
            current_a = max_a
            charge_kwh = min(fc.pv_kwh, max_charge_kwh)
            soc_sim = min(max_soc, soc_sim + (charge_kwh / cap) * 100)
            return action, current_a, soc_sim

        # Dynamisches Ziel (exakt wie in decide())
        # proj_eve mit Strombegrenzung: max. max_charge_kwh pro Stunde
        pv_rem_capped = min(pv_rem_total, max_charge_kwh * 24)
        proj_eve = min(max_soc, soc_sim + (pv_rem_capped / cap) * 100)
        if proj_eve >= target_n:
            dyn_target = target_n
        else:
            shortfall = target_n - proj_eve
            dyn_target = min(target_n + shortfall * 0.5, max_soc)

        def _apply_deficit(soc: float) -> tuple[str, float]:
            """Berechnet Deficit und setzt action: discharging wenn Ueberschuss negativ, sonst idle."""
            deficit = max(0.0, fc.consumption_kwh - fc.pv_kwh)
            new_soc = max(min_soc, soc - (deficit / cap) * 100)
            act = "discharging" if fc.net_kwh < 0 else "idle"
            return act, new_soc

        # Ziel erreicht?
        if soc_sim >= dyn_target - hyst:
            action, soc_sim = _apply_deficit(soc_sim)
            # Ping-Pong-Schutz: Deficits, die kleiner sind als die Hysterese-
            # Energie (z.B. 2% von 14 kWh = 0.28 kWh), sollen den simulierten
            # SOC nicht unter die Hysterese-Schwelle druecken. In der Realitaet
            # wuerde DVCC bei einem solchen Minimal-Deficit sofort wieder 
            # einschalten; die Simulation klemmt den SOC deshalb auf 
            # dyn_target - hyst.
            hyst_kwh = hyst / 100.0 * cap
            if fc.net_kwh >= -hyst_kwh:
                soc_sim = max(soc_sim, dyn_target - hyst)
            return action, current_a, soc_sim

        # Morgen-Verzoegerung
        if morn_s <= h < morn_e:
            if proj_eve >= target_n or soc_sim > target_n - 15:
                action, soc_sim = _apply_deficit(soc_sim)
            else:
                action = "trickle"
                current_a = self.bat.get("trickle_current", 5)
                # trickle: eigener Strom, kein PV-Ueberschuss-Limit noetig
                soc_sim = min(max_soc, soc_sim + (current_a * nom_v / 1000 / cap) * 100)
            return action, current_a, soc_sim

        # PV-Ueberschuss, begrenzt durch Strombegrenzung
        if fc.net_kwh > 0:
            action = "charging"
            current_a = min(fc.net_kwh * 1000 / nom_v, max_a)
            charge_kwh = min(fc.net_kwh, max_charge_kwh)
            soc_sim = min(max_soc, soc_sim + (charge_kwh / cap) * 100)
        else:
            action, soc_sim = _apply_deficit(soc_sim)

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

        for h in range(now_h, 24):
            # Aktuelle Stunde nur als Zukunft wenn sie noch laeuft (< 55 Min)
            if h == now_h and now_m >= 55:
                continue

            fc = fc_by_hour.get(h)
            if not fc:
                avg_cons = self.cc.get("avg_daily_consumption_kwh", 8.0) / 24
                fc = HourlyForecast(hour=h, pv_kwh=0.0, consumption_kwh=avg_cons, net_kwh=-avg_cons)

            action, current_a, soc_sim = self._simulate_hour(
                h, fc, soc_sim, needs_full, pv_rem_total, night_cons
            )

            result.append({
                "hour": h,
                "pv_kwh": fc.pv_kwh,
                "consumption_kwh": fc.consumption_kwh,
                "surplus_kwh": round(fc.net_kwh, 3),
                "action": action,
                "charge_current_a": round(current_a, 1),
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

        Erzeugt HourlyHistory-Dataclass-Objekte (keine Dictionaries!)
        """
        now = datetime.now()
        today_iso = now.date().isoformat()
        current_hour = now.hour

        # Letzten Eintrag suchen (gleiche Stunde oder vorherige)
        last_entry = None
        for i, h in enumerate(self.state.history_buffer):
            if h.date_iso == today_iso and h.hour <= current_hour:
                last_entry = h

        # Energie-Totals: aus EnergyAccumulator + persistierter Basis nach Neustart
        pv_total   = self.state.pv_energy_today_kwh   + self._energy_base_pv
        load_total = self.state.load_energy_today_kwh + self._energy_base_load

        if last_entry and last_entry.hour == current_hour:
            # Gleiche Stunde: Update mit GESAMTDIFF seit Stundenbeginn
            # (nicht nur Diff seit letztem Update!)
            pv_hour_total = max(0.0, pv_total - last_entry._hour_start_pv_total)
            cons_hour_total = max(0.0, load_total - last_entry._hour_start_cons_total)

            last_entry.pv_kwh = round(pv_hour_total, 3)
            last_entry.consumption_kwh = round(cons_hour_total, 3)
            last_entry.surplus_kwh = round(pv_hour_total - cons_hour_total, 3)
            last_entry.action = self.state.charge_mode
            last_entry.charge_current_a = self.state.charge_current_setpoint
            last_entry.soc_end = round(self.state.soc, 1)
            # Aktuelle Kumulativwerte fuer naechsten Update speichern
            last_entry._raw_pv_total  = pv_total
            last_entry._raw_cons_total = load_total

        elif last_entry and last_entry.hour < current_hour:
            # NEUE STUNDE: Letzten Eintrag der VORHERIGEN Stunde abschliessen
            # (auf den letzten bekannten Zustand setzen, falls kein Update mehr kam)
            pv_hour_total = max(0.0, last_entry._raw_pv_total - last_entry._hour_start_pv_total)
            cons_hour_total = max(0.0, last_entry._raw_cons_total - last_entry._hour_start_cons_total)
            last_entry.pv_kwh = round(pv_hour_total, 3)
            last_entry.consumption_kwh = round(cons_hour_total, 3)
            last_entry.surplus_kwh = round(pv_hour_total - cons_hour_total, 3)
            last_entry.soc_end = round(self.state.soc, 1)

            # Neuen Eintrag fuer aktuelle Stunde erstellen
            new_entry = HourlyHistory(
                date_iso=today_iso,
                hour=current_hour,
                pv_kwh=0.0,
                consumption_kwh=0.0,
                surplus_kwh=0.0,
                action=self.state.charge_mode,
                charge_current_a=self.state.charge_current_setpoint,
                soc_start=round(self.state.soc, 1),
                soc_end=round(self.state.soc, 1),
                is_actual=True,
            )
            # Stundenbeginn-Kumulativwerte speichern (inkl. Energie-Basis)
            new_entry._hour_start_pv_total = pv_total
            new_entry._hour_start_cons_total = load_total
            new_entry._raw_pv_total  = pv_total
            new_entry._raw_cons_total = load_total
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
                charge_current_a=self.state.charge_current_setpoint,
                soc_start=round(self.state.soc, 1),
                soc_end=round(self.state.soc, 1),
                is_actual=True,
            )
            new_entry._hour_start_pv_total = pv_total
            new_entry._hour_start_cons_total = load_total
            new_entry._raw_pv_total  = pv_total
            new_entry._raw_cons_total = load_total
            self.state.history_buffer.append(new_entry)

        # DEBUG: Logge letzte Stunde fuer Vergleich mit VRM Portal
        if last_entry and last_entry.hour >= 0:
            self.logger.debug(
                f"History H{last_entry.hour:02d}: PV={last_entry.pv_kwh:.3f} kWh, "
                f"Cons={last_entry.consumption_kwh:.3f} kWh, "
                f"Surplus={last_entry.surplus_kwh:.3f} kWh, "
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
        """Fuehrt einen Regelzyklus aus."""
        self._update_history()

        # ── days_since_full_charge immer aktuell aus Datum berechnen ──────────
        # Verhindert, dass der Wert nach einem langen Programmlauf (mehrere Tage
        # ohne Neustart) auf dem Stand des letzten Neustarts eingefroren bleibt.
        if self.state.last_full_charge_date:
            try:
                d = date.fromisoformat(self.state.last_full_charge_date)
                self.state.days_since_full_charge = (date.today() - d).days
            except ValueError:
                pass
        # ──────────────────────────────────────────────────────────────────────

        now = time.monotonic()
        min_dur = self._min_charge_duration_s

        # Sofort-Entscheidung bei kritischen Zuständen (Sicherheit)
        force_new = False
        if self.state.soc <= self.cc.get("emergency_charge_soc", 25):
            force_new = True
        if self._needs_full_charge() and self.state.soc < self.bat["max_soc"] - self.cc.get("soc_hysteresis", 2):
            force_new = True

        # Neue Entscheidung nur wenn Hysterese abgelaufen oder forced
        if force_new or (now - self._last_decision_ts) >= min_dur:
            target_a, mode, reason = self.decide()
            self._last_decision_ts = now
            self._last_decision_result = (target_a, mode, reason)
        else:
            target_a, mode, reason = self._last_decision_result
            # Reason ergänzen, dass Hysterese aktiv ist
            if not reason.endswith("(Hysterese)"):
                reason = reason + " (Hysterese)"

        # Schreiben nur wenn sich der gerampte Sollwert tatsaechlich geaendert hat.
        # Die frueheren Flags hysterese_abgelaufen/wert_geaendert wurden entfernt:
        # hysterese_abgelaufen war True bei jeder neuen Entscheidung, also auch
        # bei 0 A -> 0 A – das hat den Write-Schutz vollstaendig ausgehebelt.
        write_performed = False
        if target_a >= 0:
            ramped = self._ramp(target_a)
            if abs(ramped - self._last_written_ramped_a) >= 1.0:
                if self.victron.set_max_charge_current(ramped):
                    self._last_written_ramped_a = ramped
                    write_performed = True
            else:
                # Kein Schreiben: state auf letzten geschriebenen Wert belassen
                self.state.charge_current_setpoint = self._last_written_ramped_a
        # target_a ist immer >= 0 (0 = kein Laden, >0 = Ladestrom)

        self.state.charge_mode              = mode
        self.state.charge_reason            = reason
        self.state.planned_charge_schedule  = self.build_schedule()

        # Persistenter Zustand alle 30 Minuten speichern
        # (Kompromiss: SD-Karte schonen vs. max. Datenverlust nach Neustart)
        if time.monotonic() - self._last_persistent_save > 1800:
            self._save_persistent()

        if self.cfg["logging"].get("log_decisions", True):
            a = self.state.charge_current_setpoint
            log_suffix = " [KEIN WRITE]" if (target_a >= 0 and not write_performed) else ""
            self.logger.info(f"[{mode.upper()}] {a:.0f}A | {reason[:120]}{log_suffix}")


# ─────────────────────────────────────────────
# Web Dashboard (Flask, eingebettet)
# ─────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Solar Batterie Manager</title>
<style>
:root{--bg:#0f1117;--bg2:#1a1d27;--bg3:#252836;
  --acc:#f59e0b;--grn:#22c55e;--red:#ef4444;--blu:#3b82f6;--vio:#a78bfa;
  --txt:#e2e8f0;--mut:#64748b;--brd:#2d3148}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:system-ui,sans-serif;min-height:100vh}
.hd{background:var(--bg2);border-bottom:1px solid var(--brd);
    padding:12px 18px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.hd h1{font-size:1.05rem;font-weight:700;color:var(--acc)}
.hdr{display:flex;gap:14px;flex-wrap:wrap;align-items:center}
.sp{display:flex;align-items:center;gap:5px;font-size:.78rem;color:var(--mut)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--red)}
.dot.ok{background:var(--grn)}.dot.warn{background:var(--acc)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:10px;padding:14px}
.card{background:var(--bg2);border:1px solid var(--brd);border-radius:10px;padding:14px}
.lbl{font-size:.68rem;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin-bottom:5px}
.val{font-size:1.65rem;font-weight:700}
.unt{font-size:.82rem;color:var(--mut);margin-left:3px}
.sub{font-size:.73rem;color:var(--mut);margin-top:3px}
.sb{background:var(--bg3);border-radius:5px;height:7px;margin-top:7px;overflow:hidden}
.sf{height:100%;border-radius:5px;transition:width .5s}
.bdg{display:inline-block;padding:3px 9px;border-radius:20px;font-size:.7rem;font-weight:600;text-transform:uppercase}
.bc{background:rgba(34,197,94,.15);color:var(--grn);border:1px solid var(--grn)}
.bi{background:rgba(100,116,139,.15);color:var(--mut);border:1px solid var(--brd)}
.bt{background:rgba(59,130,246,.15);color:var(--blu);border:1px solid var(--blu)}
.bf{background:rgba(245,158,11,.15);color:var(--acc);border:1px solid var(--acc)}
.be{background:rgba(167,139,250,.15);color:var(--vio);border:1px solid var(--vio)}
.bd{background:rgba(239,68,68,.1);color:var(--red);border:1px solid rgba(239,68,68,.4)}
.sec{padding:0 14px 14px}
.sec h2{font-size:.88rem;font-weight:600;margin-bottom:8px}
.pan{background:var(--bg2);border:1px solid var(--brd);border-radius:10px;padding:14px;overflow-x:auto}
.chart-svg{width:100%;overflow-x:auto}
.chart-svg svg{display:block;min-width:560px}
table{width:100%;border-collapse:collapse;font-size:.78rem;min-width:540px}
th{text-align:left;padding:6px 9px;color:var(--mut);font-weight:500;border-bottom:1px solid var(--brd)}
td{padding:6px 9px;border-bottom:1px solid var(--brd)}
tr.past td{opacity:.38}tr.now td{background:rgba(245,158,11,.05)}
.rea{background:var(--bg2);border:1px solid var(--brd);border-left:3px solid var(--acc);
     border-radius:8px;padding:11px 13px;margin:0 14px 14px;font-size:.8rem;line-height:1.6}
.rl{font-size:.66rem;text-transform:uppercase;color:var(--mut);margin-bottom:3px}
.evb{background:rgba(167,139,250,.08);border:1px solid var(--vio);border-radius:8px;
     padding:9px 13px;margin:0 14px 12px;font-size:.8rem;color:var(--vio)}
.ft{text-align:center;color:var(--mut);font-size:.7rem;padding-bottom:14px}
</style>
</head>
<body>
<div class="hd">
  <h1>&#9889; Solar Batterie Manager</h1>
  <div class="hdr">
    <div class="sp"><div class="dot" id="mdot"></div><span id="mst">Verbinde...</span></div>
    <div class="sp"><div class="dot" id="edot"></div><span id="est">evcc -</span></div>
    <div class="sp" id="ts"></div>
  </div>
</div>
<div id="evb" style="display:none"></div>
<div class="grid">
  <div class="card">
    <div class="lbl">Batterie SOC</div>
    <div class="val" id="soc">-<span class="unt">%</span></div>
    <div class="sb"><div class="sf" id="sbar" style="width:0"></div></div>
    <div class="sub" id="skwh">-</div>
  </div>
  <div class="card">
    <div class="lbl">Batterie</div>
    <div class="val" id="bv">-<span class="unt">V</span></div>
    <div class="sub" id="bi">Strom: - A</div>
    <div class="sub" id="bp">Leistung: - W</div>
  </div>
  <div class="card">
    <div class="lbl">Lademodus</div>
    <div style="margin:5px 0" id="mbdg">-</div>
    <div class="sub" id="ca">-</div>
    <div class="sub" id="dfc">-</div>
  </div>
  <div class="card">
    <div class="lbl">PV Leistung</div>
    <div class="val" id="pvw">-<span class="unt">W</span></div>
    <div class="sub" id="pvk">Heute: -</div>
  </div>
  <div class="card">
    <div class="lbl">Verbrauch</div>
    <div class="val" id="ldw">-<span class="unt">W</span></div>
    <div class="sub" id="ldk">Heute: -</div>
  </div>
  <div class="card">
    <div class="lbl">Netz</div>
    <div class="val" id="grw">-<span class="unt">W</span></div>
    <div class="sub" style="color:var(--mut)">+ Bezug / - Einspeisung</div>
  </div>
  <div class="card">
    <div class="lbl">PV Prognose heute</div>
    <div class="val" id="fcp">-<span class="unt">kWh</span></div>
    <div class="sub" id="fcr">Verbleibend: -</div>
    <div class="sub" id="fcs" style="color:var(--acc)"></div>
  </div>
  <div class="card">
    <div class="lbl">Nachtverbrauch</div>
    <div class="val" id="fcn">-<span class="unt">kWh</span></div>
    <div class="sub" id="fct">Ziel-SOC: -</div>
  </div>
</div>
<div class="rea"><div class="rl">Entscheidung</div><div id="rea">Lade...</div></div>
<div class="sec">
  <h2>PV &amp; Verbrauch heute (kWh/h)</h2>
  <div class="pan chart-svg"><svg id="chart" height="220"></svg></div>
</div>
<div class="sec">
  <h2>Stundlicher Ladeplan + projizierter SOC</h2>
  <div class="pan">
    <table>
      <thead><tr><th>Uhr</th><th>PV kWh</th><th>Last kWh</th><th>Ueberschuss</th>
        <th>Aktion</th><th>Strom</th><th>SOC %</th></tr></thead>
      <tbody id="tb"></tbody>
    </table>
  </div>
</div>
<div class="ft" id="ft">-</div>
<script>
const REFRESH=__REFRESH__*1000, CAP=__CAP__;
function sc(s){return s>=80?'var(--grn)':s>=40?'var(--acc)':'var(--red)'}
function bdg(a){
  const m={charging:['Laden','bc'],trickle:['Sanft','bt'],full_charge:['Vollladung','bf'],
    idle:['Pause','bi'],discharging:['Entladen','bd']};
  const[l,c]=m[a]||['-','bi'];
  return`<span class="bdg ${c}">${l}</span>`;
}
async function refresh(){
  try{
    const d=await(await fetch('/api/state')).json();
    document.getElementById('ts').textContent=
      d.timestamp?new Date(d.timestamp).toLocaleTimeString('de'):'';
    const md=document.getElementById('mdot'),ms=document.getElementById('mst');
    if(d.modbus_connected){md.className='dot ok';ms.textContent='Modbus OK';}
    else{md.className='dot';ms.textContent='Modbus offline';}
    const ed=document.getElementById('edot'),es=document.getElementById('est');
    if(d.evcc_active){ed.className='dot warn';es.textContent='evcc '+d.evcc_mode;}
    else if(d.evcc_charge_power_w>0){ed.className='dot ok';es.textContent='evcc PV';}
    else{ed.className='dot';es.textContent='evcc -';}
    const soc=d.soc??0;
    document.getElementById('soc').innerHTML=`${soc.toFixed(1)}<span class="unt">%</span>`;
    const sb=document.getElementById('sbar');sb.style.width=soc+'%';sb.style.background=sc(soc);
    document.getElementById('skwh').textContent=`ca. ${(soc/100*CAP).toFixed(1)} kWh`;
    document.getElementById('mbdg').innerHTML=bdg(d.charge_mode);
    document.getElementById('ca').textContent=`Ladestrom: ${(d.charge_current_setpoint||0).toFixed(0)} A`;
    document.getElementById('dfc').textContent=`Vollladung vor ${d.days_since_full_charge||0} Tagen`;
    const bv=d.battery_voltage||0;
    const bi=d.battery_current||0;
    const bp=d.battery_power_w||0;
    document.getElementById('bv').innerHTML=`${bv.toFixed(2)}<span class="unt">V</span>`;
    document.getElementById('bi').textContent=`Strom: ${bi.toFixed(1)} A ${bi>0?'↑ laden':'↓ entladen'}`;
    document.getElementById('bp').textContent=`Leistung: ${bp.toFixed(0)} W`;
    document.getElementById('pvw').innerHTML=`${(d.pv_power_w||0).toFixed(0)}<span class="unt">W</span>`;
    document.getElementById('pvk').textContent=`Heute: ${(d.pv_energy_today_kwh||0).toFixed(2)} kWh`;
    document.getElementById('ldw').innerHTML=`${(d.load_power_w||0).toFixed(0)}<span class="unt">W</span>`;
    document.getElementById('ldk').textContent=`Heute: ${(d.load_energy_today_kwh||0).toFixed(2)} kWh`;
    const gw=d.grid_power_w||0;
    document.getElementById('grw').innerHTML=`${gw.toFixed(0)}<span class="unt">W</span>`;
    document.getElementById('grw').style.color=gw>50?'var(--red)':gw<-50?'var(--grn)':'var(--txt)';
    document.getElementById('fcp').innerHTML=`${(d.forecast_pv_today_kwh||0).toFixed(1)}<span class="unt">kWh</span>`;
    document.getElementById('fcr').textContent=`Verbleibend: ${(d.forecast_pv_remaining_kwh||0).toFixed(1)} kWh`;
    document.getElementById('fcn').innerHTML=`${(d.forecast_consumption_night_kwh||0).toFixed(1)}<span class="unt">kWh</span>`;
    document.getElementById('fct').textContent=`Ziel-SOC: ${(d.target_soc||80).toFixed(0)}%`;
    const srcMap={'vrm':'VRM ★','solcast':'Solcast','open_meteo':'Open-Meteo','dummy':'Dummy ⚠️','':`Open-Meteo`};
    document.getElementById('fcs').textContent=srcMap[d.forecast_source||'']||d.forecast_source||'';

    document.getElementById('rea').textContent=d.charge_reason||'-';
    const sc2=d.planned_charge_schedule||[],nH=new Date().getHours();
    (function buildChart(data,nowH){
      const svg=document.getElementById('chart');
      const W=Math.max(svg.parentElement.clientWidth-28,560);
      svg.setAttribute('width',W);
      const H=220,PAD={t:16,r:16,b:32,l:44};
      const iW=W-PAD.l-PAD.r, iH=H-PAD.t-PAD.b;
      const n=data.length||1;
      const grpW=iW/n;
      const barW=Math.max(2,grpW*0.38);
      const gap=Math.max(1,grpW*0.04);
      const maxY=Math.max(...data.map(s=>Math.max(s.pv_kwh||0,s.consumption_kwh||0)),0.5);
      // round up to nice number
      const niceMax=maxY<=1?1:maxY<=2?2:maxY<=3?3:maxY<=5?5:maxY<=6?6:Math.ceil(maxY);
      const sc=v=>iH*(1-v/niceMax);
      // Y gridlines & labels
      const ticks=[0,niceMax*0.25,niceMax*0.5,niceMax*0.75,niceMax];
      let out=`<g transform="translate(${PAD.l},${PAD.t})">`;
      // grid
      ticks.forEach(t=>{
        const y=sc(t);
        out+=`<line x1="0" y1="${y.toFixed(1)}" x2="${iW}" y2="${y.toFixed(1)}"
          stroke="rgba(255,255,255,0.07)" stroke-width="1"/>`;
        out+=`<text x="-6" y="${(y+4).toFixed(1)}" text-anchor="end"
          font-size="10" fill="#64748b">${t%1===0?t:t.toFixed(1)}</text>`;
      });
      // bars
      data.forEach((s,i)=>{
        const x=i*grpW;
        const cx=x+grpW/2;
        const isNow=(s.hour===nowH);
        const pvH=Math.max(1,iH-sc(s.pv_kwh||0));
        const ldH=Math.max(1,iH-sc(s.consumption_kwh||0));
        const pvY=sc(s.pv_kwh);
        const ldY=sc(s.consumption_kwh);
        // highlight current hour
        if(isNow) out+=`<rect x="${(x+1).toFixed(1)}" y="0" width="${(grpW-2).toFixed(1)}" height="${iH}"
          fill="rgba(245,158,11,0.07)" rx="2"/>`;
        // PV bar (left of pair)
        const pvOp=s.is_past?'0.35':'1';
        out+=`<rect x="${(cx-barW-gap/2).toFixed(1)}" y="${pvY.toFixed(1)}"
          width="${barW.toFixed(1)}" height="${pvH.toFixed(1)}"
          fill="#f59e0b" opacity="${pvOp}" rx="2">
          <title>${s.hour}:00 PV ${(s.pv_kwh||0).toFixed(2)} kWh</title></rect>`;
        // Consumption bar (right of pair)
        out+=`<rect x="${(cx+gap/2).toFixed(1)}" y="${ldY.toFixed(1)}"
          width="${barW.toFixed(1)}" height="${ldH.toFixed(1)}"
          fill="#ef4444" opacity="${s.is_past?'0.25':'0.65'}" rx="2">
          <title>${s.hour}:00 Verbrauch ${(s.consumption_kwh||0).toFixed(2)} kWh</title></rect>`;
        // hour label
        if(i%2===0||n<=14)
          out+=`<text x="${cx.toFixed(1)}" y="${(iH+18).toFixed(1)}"
            text-anchor="middle" font-size="10" fill="${isNow?'#f59e0b':'#64748b'}"
            font-weight="${isNow?'700':'400'}">${s.hour}h</text>`;
      });
      // SOC line (secondary axis, right side)
      const socData=data.filter(s=>s.projected_soc!==undefined);
      if(socData.length>1){
        const socY=v=>iH*(1-v/100);
        const pts=socData.map((s,i)=>{
          const x=i*grpW+grpW/2;
          return`${x.toFixed(1)},${socY(s.projected_soc||0).toFixed(1)}`;
        }).join(' ');
        out+=`<polyline points="${pts}" fill="none" stroke="#22c55e"
          stroke-width="1.5" stroke-dasharray="4,3" opacity="0.7"/>`;
        // SOC axis label on right
        out+=`<text x="${iW+4}" y="12" font-size="9" fill="#22c55e" opacity="0.7">SOC%</text>`;
        // SOC right axis ticks 0/50/100
        [0,50,100].forEach(t=>{
          const y=socY(t);
          out+=`<text x="${iW+4}" y="${(y+3).toFixed(1)}" font-size="9" fill="#22c55e" opacity="0.5">${t}</text>`;
        });
      }
      // Legend top-right
      out+=`<g transform="translate(${iW-160},0)">
        <rect width="10" height="10" fill="#f59e0b" rx="2" y="1"/>
        <text x="14" y="10" font-size="10" fill="#94a3b8">PV-Erzeugung</text>
        <rect x="90" width="10" height="10" fill="#ef4444" opacity="0.65" rx="2" y="1"/>
        <text x="104" y="10" font-size="10" fill="#94a3b8">Verbrauch</text>
      </g>`;
      out+=`</g>`;
      svg.innerHTML=out;
    })(sc2,nH);
    document.getElementById('tb').innerHTML=sc2.map(s=>{
      const cl=s.is_past?'past':s.hour===nH?'now':'';
      const sur=(s.surplus_kwh||0)>=0
        ?`<span style="color:var(--grn)">+${(s.surplus_kwh||0).toFixed(2)}</span>`
        :`<span style="color:var(--red)">${(s.surplus_kwh||0).toFixed(2)}</span>`;
      return`<tr class="${cl}">
        <td>${String(s.hour).padStart(2,'0')}:00</td>
        <td>${(s.pv_kwh||0).toFixed(3)}</td><td>${(s.consumption_kwh||0).toFixed(3)}</td>
        <td>${sur}</td><td>${bdg(s.action)}</td>
        <td>${s.charge_current_a>0?s.charge_current_a+'A':'-'}</td>
        <td><span style="color:${sc(s.projected_soc||0)}">${(s.projected_soc||0).toFixed(1)}%</span></td>
      </tr>`;
    }).join('');
    document.getElementById('ft').textContent=
      `Aktualisiert: ${new Date().toLocaleTimeString('de')} - naechste in ${REFRESH/1000}s`;
  }catch(e){document.getElementById('rea').textContent='Fehler: '+e.message;}
}
refresh();setInterval(refresh,REFRESH);
</script>
</body></html>
"""


def start_dashboard(cfg: dict, state: SystemState, logger: logging.Logger):
    try:
        from flask import Flask, jsonify, Response
    except ImportError:
        logger.warning("Flask fehlt - Dashboard deaktiviert  (pip install flask)")
        return

    dash    = cfg["dashboard"]
    refresh = dash.get("refresh_interval_seconds", 30)
    cap     = cfg["battery"]["capacity_kwh"]
    html    = (DASHBOARD_HTML
               .replace("__REFRESH__", str(refresh))
               .replace("__CAP__",     str(cap)))

    app = Flask(__name__)

    @app.route("/")
    def index():
        return Response(html, mimetype="text/html")

    @app.route("/api/state")
    def api_state():
        return jsonify(asdict(state))

    host = dash.get("host", "0.0.0.0")
    port = dash.get("port", 5000)
    threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False),
        daemon=True
    ).start()
    logger.info(f"Dashboard: http://{host}:{port}")


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
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    cfg    = load_config(config_path)
    logger = setup_logging(cfg)

    logger.info("=" * 60)
    logger.info("Solar Batterie Manager v2.0.6  (Modbus TCP)")
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
        start_dashboard(cfg, state, logger)

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

            # 2. Tages-Energie aufsummieren
            energy.update(state.pv_power_w, state.load_power_w)
            state.pv_energy_today_kwh  = round(energy.pv_kwh,   3)
            state.load_energy_today_kwh = round(energy.load_kwh, 3)

            # 3. evcc Status abfragen
            evcc.update()

            # 4. Prognose aktualisieren wenn faellig
            if now_ts - last_fc_ts > fc_interval:
                try:
                    fc = forecast.get_forecast()
                    state.forecast_pv_today_kwh = round(sum(f.pv_kwh for f in fc), 2)
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