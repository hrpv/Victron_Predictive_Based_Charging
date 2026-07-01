#!/usr/bin/env python3
"""Sandbox-Testlauf: Winterpause-Feature ohne Dashboard (Flask hier nicht installierbar).

Mehrere run_cycle()-Aufruefe mit gemockter Hardware, erzwungener
Winterpause (ganzes Jahr), Ausgabe direkt im Terminal.

Aufruf: python3 test_winterpause_console.py
"""
import logging
import time
from unittest.mock import MagicMock

import models
from controller import ChargeController

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("winterpause_test")

cfg = {
    "battery": {
        "capacity_kwh": 14.0, "min_soc": 35, "max_soc": 98,
        "max_charge_current": 50, "min_charge_current": 0,
        "trickle_current": 20, "full_charge_interval_days": 10,
        "voltage_nominal": 48.0,
    },
    "charging": {
        "winter_pause_enabled": True,
        "winter_pause_start": "01-01",
        "winter_pause_end": "12-31",
        "soc_hysteresis": 2,
        "emergency_charge_soc": 20,
        "morning_delay_h": 4,
    },
    "dashboard": {"state_file": "/tmp/winterpause_test_state.json"},
    "logging": {"log_decisions": True},
}

state = models.SystemState(soc=55.0, pv_power_w=1200, load_power_w=400)

forecast = MagicMock()
forecast.night_consumption_kwh.return_value = 2.0
forecast.pv_remaining_kwh.return_value = 3.0
forecast.pv_total_kwh.return_value = 6.0
forecast.get_forecast.return_value = []
forecast._calculate_sun_times.return_value = (7.0, 18.0, 12.5)  # pylint: disable=protected-access

victron = MagicMock()
victron.set_max_charge_current.return_value = True

evcc = MagicMock()

controller = ChargeController(cfg, state, forecast, victron, evcc, logger)

for i in range(3):
    print(f"\n--- Zyklus {i + 1} ---")
    controller.run_cycle()
    print(f"state.charge_mode    = {state.charge_mode}")
    print(f"state.charge_reason  = {state.charge_reason}")
    print(f"set_max_charge_current Aufrufe bisher: {victron.set_max_charge_current.call_count}")
    time.sleep(0.2)

assert state.charge_mode == "winter_pause"
assert victron.set_max_charge_current.call_count == 1, "Erwartet genau 1 einmaligen Write"
print("\nOK: Winterpause aktiv, genau 1 einmaliger Modbus-Write, danach keine weiteren Writes.")
