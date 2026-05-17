#!/usr/bin/env python3
"""
=============================================================
Prognosebasiertes Laden - Batterielebensdauer optimieren
LFP Akku | Victron Multiplus II + Cerbo GX
=============================================================
Version: 2.0.0  (Modbus TCP, kein MQTT)

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
    load_energy_today_kwh: float = 0.0      # intern aufsummiert
    grid_power_w: float = 0.0              # + = Bezug, - = Einspeisung
    battery_voltage: float = 0.0
    battery_current: float = 0.0           # + = laden, - = entladen
    battery_power_w: float = 0.0            # int16, direkt W, + = laden, - = entladen
    charge_current_setpoint: float = 0.0   # zuletzt geschriebener Wert [A]
    charge_mode: str = "idle"              # idle|charging|trickle|full_charge|evcc_priority
    charge_reason: str = ""
    last_full_charge_date: str = ""
    days_since_full_charge: int = 0
    forecast_pv_today_kwh: float = 0.0
    forecast_pv_remaining_kwh: float = 0.0
    forecast_consumption_night_kwh: float = 0.0
    planned_charge_schedule: list = field(default_factory=list)
    target_soc: float = 80.0
    modbus_connected: bool = False
    forecast_updated: str = ""
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

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("BatteryManager")
    if logger.handlers:
        return logger
    logger.setLevel(log_level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
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
        Schreibt nur bei Aenderung >= 1 A um Modbus-Kollisionen mit evcc zu minimieren.
        """
        bat = self.cfg["battery"]
        current_a = max(float(bat["min_charge_current"]),
                        min(float(bat["max_charge_current"]), current_a))
        current_a = round(current_a)

        # Schreiben nur wenn Aenderung gross genug
        if (self._last_written_a is not None
                and abs(current_a - self._last_written_a) < 1):
            return True

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
# PV-Prognose
# ─────────────────────────────────────────────

class ForecastManager:
    """Holt stundliche PV-Prognose (Open-Meteo kostenlos oder Solcast)."""

    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg = cfg
        self.logger = logger
        self.loc = cfg["location"]
        self.pv_cfg = cfg["pv"]
        self.fc_cfg = cfg["forecast"]
        self._cache: Optional[list] = None
        self._cache_ts: float = 0.0

    def get_forecast(self, force: bool = False) -> list:
        interval_s = self.fc_cfg.get("update_interval_minutes", 60) * 60
        if not force and self._cache and (time.monotonic() - self._cache_ts) < interval_s:
            return self._cache
        try:
            provider = self.fc_cfg.get("provider", "open_meteo")
            if provider == "solcast":
                fc = self._fetch_solcast()
            else:
                fc = self._fetch_open_meteo()
            self._cache = fc
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
        ns  = self.cfg["charging"].get("night_start_hour", 21)
        ne  = self.cfg["charging"].get("night_end_hour",   6)
        avg = self.cfg["charging"].get("avg_daily_consumption_kwh", 8.0) / 24
        return avg * ((24 - ns) + ne)


# ─────────────────────────────────────────────
# Energie-Tagesintegration
# ─────────────────────────────────────────────

class EnergyAccumulator:
    """Integriert Leistungsmesswerte trapezfoermig zu Tages-Energiewerten."""

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
            dt_h = (now_ts - self._last_ts) / 3600.0
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
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps({
                "last_full_charge_date":   self.state.last_full_charge_date,
                "days_since_full_charge":  self.state.days_since_full_charge,
                "timestamp":               datetime.now().isoformat()
            }, indent=2))
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

    def build_schedule(self) -> list:
        """Stundlicher Ladeplan fuer das Dashboard."""
        fc      = self.forecast.get_forecast()
        now_h   = datetime.now().hour
        soc_sim = self.state.soc
        cap     = self.bat["capacity_kwh"]
        min_soc = self.bat["min_soc"]
        max_soc = self.bat["max_soc"]
        target  = self.state.target_soc
        nom_v   = self.bat.get("voltage_nominal", 48.0)
        ns      = self.cc.get("night_start_hour", 21)
        ne      = self.cc.get("night_end_hour",   6)
        result  = []

        for f in fc:
            h         = f.hour
            action    = "idle"
            current_a = 0
            night     = h >= ns or h < ne

            if night:
                action  = "discharging"
                soc_sim = max(min_soc, soc_sim - (f.consumption_kwh / cap) * 100)
            elif f.pv_kwh > f.consumption_kwh and soc_sim < target:
                action    = "charging"
                current_a = int(min((f.net_kwh * 1000 / nom_v), self.bat["max_charge_current"]))
                soc_sim   = min(max_soc, soc_sim + (f.net_kwh / cap) * 100)
            else:
                deficit = max(0.0, f.consumption_kwh - f.pv_kwh)
                soc_sim = max(min_soc, soc_sim - (deficit / cap) * 100)

            result.append({
                "hour":             h,
                "pv_kwh":           f.pv_kwh,
                "consumption_kwh":  f.consumption_kwh,
                "surplus_kwh":      round(f.net_kwh, 3),
                "action":           action,
                "charge_current_a": current_a,
                "projected_soc":    round(soc_sim, 1),
                "is_past":          h < now_h,
            })
        return result

    def run_cycle(self):
        """Fuehrt einen Regelzyklus aus."""
        target_a, mode, reason = self.decide()

        if target_a >= 0:
            # Sanft rampen und schreiben
            ramped = self._ramp(target_a)
            self.victron.set_max_charge_current(ramped)
        # Bei target_a == -1 (evcc Prioritaet): nichts schreiben

        self.state.charge_mode              = mode
        self.state.charge_reason            = reason
        self.state.planned_charge_schedule  = self.build_schedule()

        if self.cfg["logging"].get("log_decisions", True):
            a = self.state.charge_current_setpoint
            self.logger.info(f"[{mode.upper()}] {a:.0f}A | {reason[:120]}")


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
.chart{display:flex;align-items:flex-end;gap:3px;height:150px;min-width:560px}
.bg{flex:1;display:flex;flex-direction:column;align-items:center;height:100%}
.bw{flex:1;display:flex;flex-direction:column;justify-content:flex-end;width:100%}
.b{width:100%;border-radius:3px 3px 0 0;min-height:2px}
.bpv{background:var(--acc)}.bld{background:rgba(239,68,68,.6)}
.bl{font-size:.58rem;color:var(--mut);margin-top:2px}
.nc .bpv{box-shadow:0 0 0 2px var(--acc)}
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
<div id="evb" class="evb" style="display:none">&#9889; evcc laedt das Auto - Batteriesteuerung pausiert</div>
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
  <div class="pan"><div class="chart" id="chart"></div></div>
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
    idle:['Pause','bi'],evcc_priority:['evcc','be'],discharging:['Entladen','bd']};
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
    document.getElementById('evb').style.display=d.evcc_active?'block':'none';
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
    document.getElementById('rea').textContent=d.charge_reason||'-';
    const sc2=d.planned_charge_schedule||[],nH=new Date().getHours();
    const mx=Math.max(...sc2.map(s=>Math.max(s.pv_kwh,s.consumption_kwh)),0.1);
    document.getElementById('chart').innerHTML=sc2.map(s=>{
      const ph=Math.max(2,(s.pv_kwh/mx)*138),lh=Math.max(2,(s.consumption_kwh/mx)*138);
      const nc=s.hour===nH?' nc':'';
      return`<div class="bg${nc}">
        <div class="bw"><div class="b bpv" style="height:${ph}px" title="PV ${s.pv_kwh}kWh"></div></div>
        <div class="bw"><div class="b bld" style="height:${lh}px" title="Last ${s.consumption_kwh}kWh"></div></div>
        <div class="bl">${s.hour}h</div></div>`;
    }).join('');
    document.getElementById('tb').innerHTML=sc2.map(s=>{
      const cl=s.is_past?'past':s.hour===nH?'now':'';
      const sur=s.surplus_kwh>=0
        ?`<span style="color:var(--grn)">+${s.surplus_kwh.toFixed(2)}</span>`
        :`<span style="color:var(--red)">${s.surplus_kwh.toFixed(2)}</span>`;
      return`<tr class="${cl}">
        <td>${String(s.hour).padStart(2,'0')}:00</td>
        <td>${s.pv_kwh.toFixed(3)}</td><td>${s.consumption_kwh.toFixed(3)}</td>
        <td>${sur}</td><td>${bdg(s.action)}</td>
        <td>${s.charge_current_a>0?s.charge_current_a+'A':'-'}</td>
        <td><span style="color:${sc(s.projected_soc)}">${s.projected_soc.toFixed(1)}%</span></td>
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

def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    cfg    = load_config(config_path)
    logger = setup_logging(cfg)

    logger.info("=" * 60)
    logger.info("Solar Batterie Manager v2.0  (Modbus TCP)")
    logger.info(f"Konfiguration: {config_path}")
    logger.info("=" * 60)

    state  = SystemState()
    energy = EnergyAccumulator()

    # Initiale PV-Prognose
    forecast = ForecastManager(cfg, logger)
    logger.info("Lade initiale PV-Prognose...")
    try:
        fc = forecast.get_forecast(force=True)
        state.forecast_pv_today_kwh = round(sum(f.pv_kwh for f in fc), 2)
        state.forecast_updated      = datetime.now().isoformat()
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

    # evcc Monitor
    evcc = EvccMonitor(cfg, state, victron, logger)
    if cfg.get("evcc", {}).get("enabled", False):
        logger.info(f"evcc Monitor: {cfg['evcc']['api_url']}")
    else:
        logger.info("evcc Monitor deaktiviert (evcc.enabled: false in config)")

    # Ladesteuerung
    controller = ChargeController(cfg, state, forecast, victron, evcc, logger)

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
