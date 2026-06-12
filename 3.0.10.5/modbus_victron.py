#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
modbus_victron.py — Victron Modbus-TCP Interface
=================================================
Ausgelagert aus battery_manager.py ab v3.0.10.4.

Enthält:
  - VictronModbus : Lesen aller Messwerte, Schreiben DVCC MaxChargeCurrent

Register-Referenz (Cerbo GX, Unit-ID 100):
  843  SOC               raw       -> %
  840  Voltage           raw / 10  -> V
  841  Current (signed)  raw / 10  -> A
  842  BattPower (signed) raw      -> W
  811-813  PV L1/L2/L3           -> W
  817-819  Load L1/L2/L3         -> W
  820-822  Grid L1/L2/L3 (signed)-> W
  2900 ESS BatteryLife State     -> Enum
  2705 DVCC MaxChargeCurrent     -> A  (Schreiben)
  2901 ESS MinSoc                -> %  (Lesen, von EvccMonitor)

Importiert von: battery_manager.py, evcc.py
"""

import logging
from datetime import datetime
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from models import SystemState

try:
    from pymodbus.client import ModbusTcpClient
    from pymodbus.exceptions import ModbusException
except ImportError:
    ModbusTcpClient = None
    ModbusException = Exception


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
        "soc":       (843,  1.0,  False),   # % direkt          (mbpoll 844)
        "voltage":   (840,  0.1,  False),   # uint16 / 10 -> V  (mbpoll 841)
        "current":   (841,  0.1,  True),    # int16  / 10 -> A  (mbpoll 842)
        "batt_power":(842,  1.0,  True),    # int16, direkt W   (mbpoll 843)
        # PV-Wechselrichter AC-gekoppelt L1/L2/L3 (Victron 811/812/813 = mbpoll 812/813/814)
        "pv_l1":     (811,  1.0,  False),   # W
        "pv_l2":     (812,  1.0,  False),   # W
        "pv_l3":     (813,  1.0,  False),   # W
        # AC-Lasten L1/L2/L3 (Victron 817/818/819 = mbpoll 818/819/820)
        "load_l1":   (817,  1.0,  False),   # W
        "load_l2":   (818,  1.0,  False),   # W
        "load_l3":   (819,  1.0,  False),   # W
        # Netz L1/L2/L3 signed (Victron 820/821/822 = mbpoll 821/822/823)
        "grid_l1":   (820,  1.0,  True),    # W signed, + = Bezug, - = Einspeisung
        "grid_l2":   (821,  1.0,  True),
        "grid_l3":   (822,  1.0,  True),
        # ESS-Status (read-only)
        "ess_state": (2900, 1.0,  False),   # BatteryLife State (Enum, siehe Doku)
    }
    REG_MAX_CHARGE = 2705   # DVCC MaxChargeCurrent [A]
    UNIT_ID = 100           # Cerbo GX Modbus Unit-ID
    # ESS BatteryLife States bei denen Entladen gesperrt ist
    ESS_DISCHARGE_BLOCKED_STATES = {11}

    def __init__(self, cfg: dict, state: "SystemState", logger: logging.Logger):
        self.cfg    = cfg
        self.state  = state
        self.logger = logger
        mb = cfg["modbus"]
        self.host    = mb["host"]
        self.port    = mb.get("port", 502)
        self.timeout = mb.get("timeout_seconds", 5)
        self._last_written_a: Optional[float] = None

    def _new_client(self) -> "ModbusTcpClient":
        return ModbusTcpClient(host=self.host, port=self.port, timeout=self.timeout)

    def _read_reg(self, client: "ModbusTcpClient", addr: int,
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
            self.state.pv_power_w   = round(pv_l1 + pv_l2 + pv_l3, 0)
            self.state.load_power_w = round(l1 + l2 + l3, 0)
            self.state.grid_power_w = round(g1 + g2 + g3, 0)

            # ESS-Status lesen (Reg 2900)
            ess_state = r("ess_state")
            if ess_state is not None:
                new_ess_state = int(ess_state)
                if new_ess_state != self.state.ess_battery_life_state:
                    ess_state_names = {
                        10: "Self-consumption (SOC >= MinSOC)",
                        11: "SOC below MinSOC -> Entladen GESPERRT",
                        12: "Recharge (SOC >5% unter MinSOC)",
                    }
                    desc = ess_state_names.get(new_ess_state, str(new_ess_state))
                    self.logger.info(
                        f"ESS BatteryLife State geaendert: "
                        f"{self.state.ess_battery_life_state} -> {new_ess_state} ({desc})")
                self.state.ess_battery_life_state = new_ess_state

            self.state.timestamp = datetime.now().isoformat()
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

        # Schicht-1: Shadow-Variable – identischer Wert -> kein Write
        if self._last_written_a is not None and self._last_written_a == current_a:
            self.state.charge_current_setpoint = current_a
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
