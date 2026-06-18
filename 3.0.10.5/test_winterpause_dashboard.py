#!/usr/bin/env python3
"""Sandbox-Testlauf: Winterpause-Feature live im Dashboard beobachten.

Startet den ChargeController mit gemockter Hardware (kein echtes
Victron/evcc/Internet noetig) im erzwungenen Winterpause-Zeitraum und
hebt ein EIGENES Flask-Dashboard auf Port 5001 (NICHT 5000!) - so kann
dieses Testskript parallel zum echten battery_manager.py (Port 5000)
laufen, ohne dessen Port zu belegen.

Aufruf: python3 test_winterpause_dashboard.py
Dashboard: http://localhost:5001
"""
import logging
import threading
import time
from unittest.mock import MagicMock

import models
from controller import ChargeController
from dashboard import start_dashboard
from logging_setup import DeduplicatingFilter

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
        # Zeitraum bewusst auf das ganze Jahr gesetzt, damit der Test
        # unabhaengig vom aktuellen Datum sofort die Winterpause zeigt.
        "winter_pause_enabled": True,
        "winter_pause_start": "01-01",
        "winter_pause_end": "12-31",
        "soc_hysteresis": 2,
        "emergency_charge_soc": 20,
        "morning_delay_h": 4,
    },
    "dashboard": {
        "enabled": True, "host": "0.0.0.0", "port": 5001,
        "refresh_interval_seconds": 3,
        "state_file": "/tmp/winterpause_test_state.json",
    },
    "logging": {"log_decisions": True, "dedup_enabled": False},
}

state = models.SystemState(soc=55.0, pv_power_w=1200, load_power_w=400)

forecast = MagicMock()
forecast.night_consumption_kwh.return_value = 2.0
forecast.pv_remaining_kwh.return_value = 3.0
forecast.pv_total_kwh.return_value = 6.0
forecast.get_forecast.return_value = []
forecast._calculate_sun_times.return_value = (7.0, 18.0, 12.5)

victron = MagicMock()
victron.set_max_charge_current.return_value = True

evcc = MagicMock()

controller = ChargeController(cfg, state, forecast, victron, evcc, logger)

dedup_stream = DeduplicatingFilter(enabled=False)
start_dashboard(cfg, state, logger, dedup_stream, version="WINTERPAUSE-TEST")


def cycle_loop():
    while True:
        controller.run_cycle()
        time.sleep(5)


threading.Thread(target=cycle_loop, daemon=True).start()

logger.info("Winterpause-Testlauf aktiv. Dashboard: http://localhost:5001 (NICHT 5000)")
while True:
    time.sleep(60)
