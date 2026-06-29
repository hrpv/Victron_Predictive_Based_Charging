#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# pylint: disable=logging-fstring-interpolation,too-many-instance-attributes
# pylint: disable=too-few-public-methods,broad-exception-caught
"""
evcc.py — evcc Koordination für Solar Batterie Manager
=======================================================
Ausgelagert aus battery_manager.py ab v3.0.10.4.

Enthält:
  - EvccMonitor : REST-Polling + Reg-2901-Überwachung

Korrekte evcc-Registerbelegung (Victron Modbus TCP):
  Register 2901  ESS MinSoc  [%]
    - Normalzustand:   10–20 %  (konfigurierter Min-SOC)
    - Schnellladen:    ≈ aktueller SOC  (verhindert Akkuentladung)
    - Nach Schnellladen: wieder 10–20 %

  Register 2705  DVCC MaxChargeCurrent  [A]
    - evcc schreibt dieses Register NICHT
    - Wird ausschliesslich von battery_manager gesteuert
    -> Kein Schreibkonflikt auf Reg 2705

Importiert von: battery_manager.py
"""

import logging
import time
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from models import SystemState
    from modbus_victron import VictronModbus


# ─────────────────────────────────────────────
# evcc Koordination
# ─────────────────────────────────────────────

class EvccMonitor:
    """
    Ueberwacht evcc per REST API.

    Was wir aus evcc ableiten:
      - evcc_active:           Auto wird gerade geladen (info)
      - evcc_mode:             "off"|"now"|"minpv"|"pv"
      - evcc_charge_power:     Ladeleistung Wallbox [W]
      - evcc_discharge_locked: Reg 2901 > normaler MinSoc
                                -> evcc sperrt Akkuentladung
                                -> wir duerfen laden, aber SOC-Ziel
                                   nicht unter evcc-MinSoc senken

    Einfluss auf Ladesteuerung:
      - evcc_discharge_locked = True:
          Unser effektiver SOC-Mindestwert = evcc_min_soc (aus Reg 2901)
          statt dem konfigurierten battery.min_soc.
          Laden laeuft normal weiter – kein Register-Konflikt.
    """

    REG_MIN_SOC = 2901      # ESS MinSoc [%], gelesen via Modbus
    NORMAL_MIN_SOC_MAX = 25 # Alles darueber gilt als "evcc hat gesperrt"

    def __init__(self, cfg: dict, state: "SystemState",
                 victron: "VictronModbus", logger: logging.Logger):
        self.cfg     = cfg
        self.state   = state
        self.victron = victron
        self.logger  = logger
        evcc_cfg = cfg.get("evcc", {})
        self.enabled        = evcc_cfg.get("enabled", False)
        self.url            = evcc_cfg.get("api_url", "http://localhost:7070/api/state")
        self.timeout        = evcc_cfg.get("timeout_seconds", 5)
        self._poll_interval = evcc_cfg.get("poll_interval_seconds", 30)
        self._last_check: float = 0.0
        self.evcc_min_soc: float = 0.0

    def update(self) -> None:
        """Liest evcc-Status (REST) und MinSoc-Register (Modbus)."""
        if not self.enabled:
            self.state.evcc_active           = False
            self.state.evcc_discharge_locked = False
            return

        now = time.monotonic()
        if now - self._last_check < self._poll_interval:
            return
        self._last_check = now

        # ── MinSoc aus Modbus Register 2901 lesen ─────────────
        min_soc_reg = self.victron.read_register(
            self.REG_MIN_SOC, scale=0.1)  # Rohwert * 10 = %, also scale=0.1
        if min_soc_reg is not None:
            self.evcc_min_soc = float(min_soc_reg)
            self.state.evcc_min_soc = self.evcc_min_soc
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
