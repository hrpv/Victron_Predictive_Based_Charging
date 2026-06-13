# Changelog — Solar Batterie Manager

Victron ESS / Multiplus II + Cerbo GX | Modbus TCP | Predictive Charging

---

## v3.0.10.5 — Code-Review Cleanup (2026-06-12)

Changed:
- `battery_manager.py`: Veralteten Header aktualisiert (v3.0.10.0 → v3.0.10.5,
  Dateistruktur zeigt jetzt alle 8 Module).
- `battery_manager.py`: Überflüssige Imports entfernt — `HourlyForecast`,
  `HourlyHistory` (nirgends verwendet), `DeduplicatingFilter` (nur
  `setup_logging()` nötig, Instanz wird zurückgegeben).
- `battery_manager.py`: 6 Migrationskommentar-Blöcke entfernt (Relikte des
  Refactorings, kein Mehrwert nach Abschluss der Aufteilung).
- `battery_manager.py`: Guard `if dedup_stream is not None` vor
  `start_dashboard()`-Aufruf (defensiv gegen theoretischen Doppel-Init).
- `dashboard.py`: `TYPE_CHECKING`-Import korrigiert:
  `from battery_manager import ...` → `from models import SystemState` /
  `from logging_setup import DeduplicatingFilter` (verhindert zirkulären
  Import bei aktiviertem Type-Checker).
- `dashboard.py`: Ungenutzten `import re as _re` entfernt (Copy-Paste-Relikt
  aus `DeduplicatingFilter._normalize()`).
- `forecast.py`: Tote Methode `_sundown_unix()` entfernt (obsolet seit
  `_get_dynamic_night_window()` astronomische Zeiten berechnet).
- `modbus_victron.py`: Ungenutzten `ModbusException`-Import entfernt
  (alle Fehler werden durch generisches `except Exception` abgefangen).
- `controller.py`: Doppelten `# Hauptprogramm`-Kommentar-Header und
  veralteten Heartbeat-Erklärungskommentar am Dateiende entfernt
  (Heartbeat lebt seit Refactoring in `dashboard.py`).

---

## v3.0.10.5 (2026-06-12)

Changed:
- `controller.py` eingeführt: `EnergyAccumulator`, `PowerSmoother`, `ChargeController`
  ausgelagert (~1190 Zeilen).
- `battery_manager.py` ist jetzt reiner Glue-Code (360 Zeilen): nur noch `main()`,
  `load_config()`, `validate_config()`, `_forecast_source()` und Imports.
- Nicht mehr benötigte Imports entfernt: `json`, `re`, `math`, `logging.handlers`,
  `threading`, `deque`, `asdict`, `timedelta`, `timezone`, `date`.
- VERSION auf 3.0.10.5 aktualisiert.

Fixed:
- `controller.py`: `from __future__ import annotations` ergänzt (Zeile 3).
  Ohne diesen Import wertet Python Typ-Annotationen in `ChargeController.__init__()`
  zur Laufzeit aus — `VictronModbus` und `EvccMonitor` standen nur im
  `TYPE_CHECKING`-Block und waren zur Laufzeit undefiniert → `NameError`.
  Mit `from __future__ import annotations` werden alle Annotationen lazy
  als Strings behandelt und nie ausgewertet (PEP 563, Python 3.7+).

---

## v3.0.10.4 (2026-06-12)

Changed:
- `modbus_victron.py` eingeführt: `VictronModbus` ausgelagert inkl. pymodbus-Import
  (try/except für pymodbus 3.x / 2.x Fallback).
- `evcc.py` eingeführt: `EvccMonitor` ausgelagert.
- `battery_manager.py`: pymodbus try/except-Block entfernt (nur noch in `modbus_victron.py`).
- `from modbus_victron import VictronModbus` und `from evcc import EvccMonitor` neu.
- VERSION auf 3.0.10.4 aktualisiert.

---

## v3.0.10.3 (2026-06-12)

Changed:
- `forecast.py` eingeführt: `VrmForecastManager` und `ForecastManager` ausgelagert
  (inkl. `_calculate_sun_times`, `_get_dynamic_night_window`).
- `battery_manager.py`: `from forecast import ForecastManager` neu.
- `import math` bleibt in `battery_manager.py` (wird in `ChargeController._is_night()`
  via `math.ceil`/`math.floor` noch benötigt).
- `HourlyForecast` weiterhin via `from models import` verfügbar (in `build_schedule()` gebraucht).
- VERSION auf 3.0.10.3 aktualisiert.

---

## v3.0.10.2 (2026-06-12)

Changed:
- `logging_setup.py` eingeführt: `DeduplicatingFilter` und `setup_logging()` ausgelagert.
- `battery_manager.py`: `import re` entfernt (nur noch in `logging_setup.py` gebraucht),
  `import os` explizit hinzugefügt (weiterhin in `_save_persistent()` gebraucht).
- `from logging_setup import DeduplicatingFilter, setup_logging` neu.
- VERSION auf 3.0.10.2 aktualisiert.

---

## v3.0.10.1 (2026-06-12)

Changed:
- `models.py` eingeführt: `SystemState`, `HourlyForecast`, `HourlyHistory` nach
  `models.py` ausgelagert. Keine Logikänderung.
- `EnergyAccumulator` und `PowerSmoother` bleiben in `battery_manager.py`
  (haben update()-/reset()-Logik, kein reines Datenmodell).
- `battery_manager.py`: `from models import SystemState, HourlyForecast, HourlyHistory`
  ersetzt die lokalen Klassendefinitionen. `dataclass`/`field`-Import entfernt.
- VERSION auf 3.0.10.1 aktualisiert.

---

## v3.0.10.0 (2026-06-12)

Changed:
- Datei aufgeteilt in `battery_manager.py`, `dashboard.py`, `CHANGELOG.md`.
- `VERSION`-Konstante eingeführt: ein einziger Ort für alle Versionsstrings
  (GUI-Titel, h1, logger.info, Datei-Header).
- `DASHBOARD_HTML` und `start_dashboard()` nach `dashboard.py` ausgelagert.
- Changelogs aus Quellcode entfernt und in diese Datei überführt.

---

## v3.0.9.28 (2026-06-12)

Fixed:
- `run_cycle()`: `"(Hysterese)"` wurde an alle gecachten Entscheidungen
  angehängt, nicht nur an Warte-Entscheidungen (`mode="idle"`).

  Alt:
  ```python
  elif not reason.endswith("(Hysterese)"):
      reason = reason + " (Hysterese)"
  ```
  Neu:
  ```python
  elif mode == "idle" and not reason.endswith("(Hysterese)"):
      reason = reason + " (Hysterese)"
  ```

  Begründung: Das Suffix `"(Hysterese)"` signalisiert dem Nutzer dass die
  Entscheidung aus dem Cache stammt (kein neuer `decide()`-Aufruf wegen
  `min_decision_interval`). Bei `mode="charging"` oder `"full_charge"` ist
  der Zusatz semantisch falsch und suggeriert fälschlicherweise einen
  SOC-Hysterese-Wartemodus.

---

## v3.0.9.27 (2026-06-12)

Fixed:
- `decide()`: `pv_in_optimal` verwendete `f.pv_kwh` (Brutto-PV) statt
  Netto-Überschuss (PV − Verbrauch). Dadurch wurde die Warteentscheidung
  "PV im Optimal-Fenster ausreichend" gegenüber dem tatsächlich in den
  Akku fließenden Strom zu optimistisch.

  Alt:
  ```python
  pv_in_optimal = sum(f.pv_kwh for f in fc_list if opt_start <= f.hour <= opt_end)
  if pv_in_optimal >= needed_kwh and soc >= min_required:
      return 0, "idle", "... warte"
  ```
  Neu:
  ```python
  net_in_optimal = sum(max(0.0, f.net_kwh) for f in fc_list
                       if opt_start <= f.hour <= opt_end)
  if net_in_optimal >= needed_kwh and soc >= min_required:
      return 0, "idle", "... warte"
  ```

  Begründung: `needed_kwh` ist die Netto-Energie die der Akku benötigt
  (SOC-Delta × Kapazität). Der Vergleichswert muss ebenfalls Netto sein.
  Beispiel: PV 11–15 Uhr = 6,5 kWh, Verbrauch = 3,8 kWh → netto 2,7 kWh.
  Ziel-Energie: 5,9 kWh. Vorher: 6,5 >= 5,9 → warte (falsch).
  Nachher: 2,7 < 5,9 → frühes Laden nötig (korrekt).

- `_simulate_hour()`: Morgen-Fenster (`h < opt_start`, `soc >= min_required`)
  wartete immer ohne zu prüfen ob das Optimal-Fenster die benötigte
  Netto-Energie tatsächlich liefert. Inkonsistenz zu `decide()`.

  Neu: `net_in_opt`-Check analog zu `decide()` eingebaut, damit Entscheidung
  und Ladeplan übereinstimmen.

---

## v3.0.9.26 (2026-06-11)

Changed:
- `decide()`: Optimal-Fenster-Sollwert wird jetzt auf konfigurierbare
  Stromstufen quantisiert (`charging.optimal_window_current_step_a`, Default 5 A).
  Begründung: `surplus_w` schwankt um ±2000 W → ohne Quantisierung ändert sich
  `charge_a` im Minutentakt (18/19/20 A), obwohl physikalisch kein Unterschied besteht.

- `run_cycle()`: Schreib-Hysterese im Optimal-Fenster auf
  `charging.optimal_window_write_deadband_a` angehoben (Default 3 A).
  Netto-Effekt: Flash-Schreibrate sinkt von ~6–8 auf < 2 Writes/Stunde.

---

## v3.0.9.25_fixed (2026-06-11)

Fixed:
- `_simulate_hour()`: PV-Überschuss-Block außerhalb Optimal-Fenster
  war inkonsistent mit `decide()`. `decide()` setzt bei `soc < dyn_target`
  `max_a` ohne Netz-kWh-Cap — ESS/DVCC begrenzen physikalisch.

---

## v3.0.9.25 (2026-06-11)

Changed:
- `decide()`: Pfad 6 (PV-Überschuss außerhalb Optimal-Fenster) und
  Pfad 7 (Trickle) entfernt, ersetzt durch einfachen Block:
  `soc < dyn_target → charge_a = max_a, mode="charging"`.
  Begründung: 200W-Schwelle verursachte ständiges Flackern (3A ↔ 10A)
  bei wolkenbedingten Schwankungen. Victron ESS/DVCC begrenzen automatisch.

---

## v3.0.9.24 (2026-06-10)

Changed:
- `_simulate_hour()`: bei `action=idle` und `SOC > floor_soc` wird jetzt
  `current_a = min_charge_current` (z.B. 3 A) statt 0,0 A verwendet.
  Physikalisch korrekt: Reg. 2705 steht auch im idle-Zustand auf
  mindestens `min_charge_current`. SOC steigt leicht (~1 %/h bei 3 A / 48 V / 100 Ah).
  Ausnahme: `SOC <= floor_soc` → `current_a=0`, SOC eingefroren (ESS State 11/12).
- `_apply_deficit()` gibt jetzt 3-Tupel `(action, current_a, new_soc)` zurück
  (vorher 2-Tupel). Alle internen Aufrufe angepasst.

---

## v3.0.9.23 (2026-06-10)

Fixed:
- `build_schedule()`: `planned_current_a` universell korrekt berechnet.
  Formel: `min(surplus_current_a, max(current_a, min_charge_current))` für alle Stunden.
  Bisher wurde bei idle-Stunden mit positivem Überschuss `surplus_current_a`
  ungecappt ausgegeben (z.B. +28 A statt +3 A).

---

## v3.0.9.22 (2026-06-09)

Changed:
- Ladeplanung: `charge_current_a` zeigt jetzt tatsächlichen/erwarteten
  Stromfluss (signed) statt Reg-2705-Setpoint.
  Vergangenheit: Integration Reg. 842 (`battery_power_w`) → Wh / nom_v = mittlerer Strom [A].
  Zukunft: `min(surplus_kwh * 1000 / nom_v, setpoint_a)`.
- `EnergyAccumulator`: neues Feld `bat_wh` (signed Wh, + = Laden).
- `HourlyHistory`: neue Felder `_hour_start_bat_wh`, `bat_energy_wh`.
- Dashboard: Spalte "Strom" mit Vorzeichen, grün/rot für Laden/Entladen.

---

## v3.0.9.21 (2026-06-09)

Fixed:
- `_simulate_hour()`: verwendete `max(0.0, ...)` statt `max(floor_soc, ...)`
  im Notfall-SOC-Block → Simulation unterschritt Reg 2901 ESS MinimumSocLimit.

---

## v3.0.9.20 (2026-06-08)

Fixed:
- Trickle-Pfad griff auch bei vorhandenem PV-Überschuss: `decide()` Pfad 7
  hatte keinen Überschuss-Check. Fix: Guard `raw_surplus_w < 200 W`.
- Hysterese fror falsche Entscheidung ein: `force_new` jetzt auch bei
  `grid_power_w < -1000 W` (massiver Export) und bei evcc-Statuswechsel.

---

## v3.0.9.19 (2026-06-07)

Fixed:
- Heartbeat-Thread erhielt `NameError` weil `dedup_stream` nicht in
  `start_dashboard()` sichtbar war. Fix: als Parameter übergeben.
  Ergebnis: Journal zeigt alle 20 Minuten `[IDLE]`-Heartbeat unabhängig
  von Browser-Aktivität.
