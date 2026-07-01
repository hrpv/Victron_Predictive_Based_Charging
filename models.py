#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# pylint: disable=too-many-instance-attributes
"""
models.py — Datenklassen für Solar Batterie Manager
=====================================================
Ausgelagert aus battery_manager.py ab v3.0.10.1.

Enthält ausschliesslich reine Datenklassen (dataclasses) ohne Logik:
  - SystemState     : Aktueller Systemzustand (Modbus + Steuerung + Prognose)
  - HourlyForecast  : Stündliche PV-Prognose (Eingang für ForecastManager)
  - HourlyHistory   : Tatsächlicher Stundenverlauf (History-Ringpuffer)

Bewusst NICHT hier:
  - EnergyAccumulator : Hat update()-Logik -> gehört zu controller.py
  - PowerSmoother     : Hat Glättungslogik  -> gehört zu controller.py

Importiert von: battery_manager.py, controller.py, forecast.py,
                modbus_victron.py, evcc.py, dashboard.py
"""

from dataclasses import dataclass, field


# ─────────────────────────────────────────────
# Systemzustand
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
    grid_power_w: float = 0.0               # + = Bezug, - = Einspeisung
    battery_voltage: float = 0.0
    battery_current: float = 0.0            # + = laden, - = entladen
    battery_power_w: float = 0.0            # int16, direkt W, + = laden, - = entladen
    bat_energy_today_wh: float = 0.0        # signed Wh heute (EnergyAccumulator.bat_wh)
    charge_current_setpoint: float = 0.0    # zuletzt geschriebener Wert [A]
    charge_mode: str = "idle"               # idle|charging|full_charge
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
    forecast_source: str = ""               # vrm | solcast | open_meteo | dummy
    evcc_active: bool = False               # Auto wird gerade geladen (Info)
    evcc_discharge_locked: bool = False     # evcc hat Reg 2901 erhoeht -> kein Entladen
    evcc_mode: str = ""                     # "off"|"now"|"minpv"|"pv"
    evcc_charge_power_w: float = 0.0        # Wallbox-Ladeleistung [W]
    evcc_min_soc: float = 0.0              # Aktueller Wert in Reg 2901 [%]
    ess_battery_life_state: int = -1        # Reg 2900: BatteryLife State (-1 = unbekannt)


# ─────────────────────────────────────────────
# Prognose
# ─────────────────────────────────────────────

@dataclass
class HourlyForecast:
    """Stündliche PV-Prognose mit Verbrauch und Netto-Überschuss."""
    hour: int
    pv_kwh: float
    consumption_kwh: float
    net_kwh: float                          # positiv = Ueberschuss


# ─────────────────────────────────────────────
# History
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
    # Kumulativwerte zu Stundenbeginn (fuer korrekte Stundensumme)
    _hour_start_pv_total: float = 0.0
    _hour_start_cons_total: float = 0.0
    # Batteriestrom-Integration (Reg. 842): signed Wh pro Stunde
    # charge_current_a wird am Stundenende daraus berechnet: bat_energy_wh / nom_v
    bat_energy_wh: float = 0.0            # integrierter Batterieenergiefluss [Wh], signed
    _hour_start_bat_wh: float = 0.0       # EnergyAccumulator.bat_wh zu Stundenbeginn
    _raw_bat_wh: float = 0.0              # letzter EnergyAccumulator.bat_wh-Wert
