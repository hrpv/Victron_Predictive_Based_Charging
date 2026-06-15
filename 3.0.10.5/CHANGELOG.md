# Changelog — Solar Batterie Manager

Victron ESS / Multiplus II + Cerbo GX | Modbus TCP | Predictive Charging

---

## v3.0.11.1 — Bugfix: Phantomstrom bei Stundenbeginn (2026-06-15)

Fixed:
- `controller.py` (`_update_history`): In den ersten Minuten einer neuen
  Stunde wurde `charge_current_a` durch ein winziges `elapsed_h` dividiert,
  was Phantomwerte erzeugte (z.B. **-69.3 A** bei 00:00 statt der erwarteten
  ~6 A). Ursache: `elapsed_h = minute/60 + second/3600` ist bei xx:00:30
  ca. 0.0083 h; der frühere Schutz `max(elapsed_h, 1/60)` klemmte nur auf
  1 Minute, was den Fehler noch um Faktor 5 magnifizierte.

  Fix: Unter 5 Minuten (`elapsed_h < 5/60`) wird `charge_current_a = 0.0`
  gesetzt. In diesem Zeitraum ist die Anzeige ohnehin nicht aussagekräftig;
  ab Minute 5 greift die normale Berechnung. Die abgeschlossene Stunde
  (Stundenabschluss-Pfad, `elapsed_h = 1.0`) ist nicht betroffen.

- `version.py`: VERSION auf 3.0.11.1 aktualisiert.

---

## v3.0.11 — Optimal-Fenster: Prognose-basierte Stundensteuerung (2026-06-14)

Changed:
- `controller.py`: Optimal-Fenster-Logik grundlegend neu geschrieben.

  **Alt (v3.0.10.x):** Ladestrom wurde jeden Zyklus aus dem Momentan-Überschuss
  (Grid-Messung) berechnet, mit Quantisierung (5A-Stufen), `_smooth_required_a`-
  Filter (3-Zyklen-Mittelwert) und `write_deadband` (3A) gegen Modbus-Flood.
  Ursache aller Komplexität: Grid-Messung rauscht ±2000W → Strom schwankte
  ständig, musste künstlich stabilisiert werden.

  **Neu (v3.0.11):** Strom wird aus der **Prognose** und dem **Bedarf bis
  Ziel-SOC** gesetzt — kein Momentanwert, kein Filter, kein Deadband.

  Steuerprinzip:
  - **Stundenbeginn** (Fenstereintritt oder volle Stunde xx:00):
    ```
    missing_wh  = (dyn_target - soc) / 100 * capacity_wh
    needed_wh   = missing_wh / hours_left          # gleichmäßig verteilen
    planned_wh  = min(forecast_surplus_wh,         # nie mehr als PV liefert
                      needed_wh + deficit_share_wh)
    charge_a    = planned_wh / battery_voltage     # clamp: min_a..max_a
    ```
    `dyn_target` ist echte Steuergröße: wenig Reststunden → höherer Strom,
    Ziel fast erreicht → niedrigerer Strom. Kein fester `reduced_charge_current_a`
    mehr nötig.
  - **Innerhalb der Stunde:** Strom bleibt konstant, Rampe läuft schrittweise
    zum neuen Zielwert. Kein Modbus-Write wenn Rampe abgeschlossen.
  - **SOC-Guard** (`soc > dyn_target`): sofort auf `min_charge_current`,
    Plan wird zurückgesetzt.
  - **Stundenwechsel – Defizit-Ausgleich:**
    Tatsächlich geladene Energie (`bat_wh_total`, signed — Entladung zählt mit)
    wird mit dem Plan verglichen. Defizit kumuliert, beim nächsten Stundenbeginn
    auf Reststunden verteilt und zum Bedarf addiert:
    ```
    deficit_wh    = planned_wh - actual_wh        # signed
    carried_wh   += deficit_wh
    deficit_share = carried_wh / hours_left       # nächste Stunde
    ```
  - **Rampe:** Stromänderungen weiterhin schrittweise (+/- `current_ramp_step`
    A/Zyklus). Hysterese 1A verhindert Modbus-Write wenn Rampe abgeschlossen.
  - **Midnight-Reset:** `_opt_plan_hour`, `_opt_carried_wh` etc. bei
    Tageswechsel geleert.

  Entfernte Config-Keys (können in `config.yaml` stehen bleiben, werden
  stillschweigend ignoriert):
  - `optimal_window_write_deadband_a`
  - `optimal_window_current_step_a`
  - `required_a_smooth_window`
  - `optimal_window_min_current_a`

  Log-Beispiel (Normalbetrieb, SOC 46%→79%, 5h Fenster):
  ```
  Optimal-Fenster H11 neuer Plan: Prognose=5336Wh, Bedarf=924Wh/h (4620Wh/5h), Defizitanteil=+0Wh, Plan=924Wh -> 19.2A
  [CHARGING] 19A | Optimal-Fenster H11: 19A (Plan 924Wh, Übertrag +0Wh, SOC 46.0%→79%)
  Optimal-Fenster H11 abgeschlossen: Plan=924Wh, Ist=900Wh, Defizit=+24Wh, Übertrag=+24Wh
  Optimal-Fenster H12 neuer Plan: Prognose=5571Wh, Bedarf=924Wh/h (3696Wh/4h), Defizitanteil=+6Wh, Plan=930Wh -> 19.4A
  ```

Fixed (während Live-Test 2026-06-14 entdeckt):
- **Rampe im Optimal-Fenster fehlte** (v3.0.11-Erbschaft aus v3.0.10.7):
  `run_cycle()` setzte `ramped = target_a` direkt statt `_ramp(target_a)`.
  Strom sprang sofort statt schrittweise. Fix: einheitliche Rampe für alle
  Modi, `is_optimal`-Sonderbehandlung im Write-Block entfernt.
- **Ladestrom klebte bei max_a** (50A): Ursprüngliche Formel ignorierte
  `dyn_target` — `planned_wh = forecast_surplus_wh` ergab bei 5-6 kWh
  Prognose immer >2400Wh → immer 50A. Fix: `planned_wh` auf `needed_wh`
  (Bedarf bis Ziel-SOC pro Reststunde) gedeckelt, Prognose als Obergrenze.

- **Ladeplan zeigt echten Sollwert für laufende Stunde**: `build_schedule()`
  verwendete für die aktuelle Stunde den simulierten Strom aus
  `_simulate_hour()`, der keine Defizit-Korrekturen aus Vorjahr-Stunden kennt.
  Fix: wenn `_opt_plan_hour == now_h`, wird `_opt_setpoint_a` direkt eingesetzt.
  Nach Stundenabschluss greift wie bisher der History-Wert (`bat_energy_wh`-
  integrierter Ist-Strom). Kein Dashboard-Update nötig.

- `dashboard.py`: Stromwerte im Ladeplan ohne Vorzeichen für laufende und
  zukünftige Stunden (Sollwerte/Prognose). Vergangene Stunden zeigen weiterhin
  vorzeichenbehafteten Ist-Strom (`+9.3 A` laden / `-16.7 A` entladen).
  Nur `dashboard.py` betroffen, kein `controller.py`-Update.

- `version.py`: VERSION auf 3.0.11 aktualisiert.

---

## v3.0.10.7 — Write-Hysterese Regression-Fix (2026-06-14)

Fixed:
- `controller.py` (v3.0.10.6 Regression): Die reine Hysterese auf
  `target_a` brach bei **Idle/Nacht** und **Morgen-Notladung/Nachmittag**.

  Ursache: `target_a = 0` bei Idle wurde einmalig mit dem gerampeten
  Zwischenwert (z.B. 40A) geschrieben, weil `abs(0 - 50) >= 1` zutraf.
  Danach wurde `_last_quantized_target_a = 0` gesetzt. Alle folgenden
  Zyklen sahen `abs(0 - 0) = 0 < 1` → **kein Write**. `_ramp_current`
  lief weiter herunter (35, 30, 25...A), aber nie wieder an den Modbus.
  Ergebnis: Setpoint blieb auf 40A statt 0A (oder min_charge_current).

  Beobachtet im Log (2026-06-13 22:03 ff.):
  ```
  [IDLE] 40A | Nacht: kein Laden (SOC 61.0%)
  [IDLE] 40A | Nacht: kein Laden (SOC 61.0%) (Hysterese) [KEIN WRITE]
  ...
  [IDLE] 40A | Nacht: kein Laden (SOC 52.0%) (Hysterese) [KEIN WRITE]
  ```

  Fix: Kombinierte Hysterese:
  - **Optimal-Fenster** (`mode="charging"` + `"Optimal-Fenster" in reason`):
    Hysterese auf `target_a` (wie v3.0.10.6), aber mit **sofortigem Rampen**
    (`ramped = target_a`, `_ramp_current = target_a`). Verhindert, dass
    ein gerampeter Zwischenwert geschrieben und eingefroren wird.
  - **Alle anderen Modi** (Idle, Nachmittag, Morgen-Notladung, Trickle,
    Full-Charge): Hysterese auf **gerampetem Wert** (wie vor v3.0.10.6).
    Schrittweises Herunter-/Hochrampen funktioniert korrekt.

  Alt (v3.0.10.6, defekt):
  ```python
  if target_a >= 0:
      ramped = self._ramp(target_a)
      if abs(target_a - self._last_quantized_target_a) >= write_threshold:
          ...
  ```
  Neu (v3.0.10.7):
  ```python
  is_optimal = mode == "charging" and "Optimal-Fenster" in reason
  if is_optimal:
      ramped = target_a          # sofortiges Rampen
      self._ramp_current = target_a
  else:
      ramped = self._ramp(target_a)  # schrittweises Rampen

  if is_optimal:
      should_write = abs(target_a - self._last_quantized_target_a) >= threshold
  else:
      should_write = abs(ramped - self._last_written_ramped_a) >= threshold
  ```

Changed:
- `version.py`: VERSION auf 3.0.10.7 aktualisiert.

---

## v3.0.10.6 — Optimal-Fenster Write-Stabilisierung (2026-06-13)

Fixed:
- `controller.py` (Fix 1 — Kimi): `run_cycle()`: Schreib-Hysterese prüfte den
  **gerampten** Wert (`ramped`) statt des **quantisierten Sollwerts** (`target_a`).

  `_ramp()` steigert den Strom in Schritten pro Zyklus. Wenn `decide()` zwischen
  10A und 15A oszillierte, durchlief `_ramp_current` bei jedem Wechsel die
  Zwischenstufen. Die Hysterese `abs(ramped - last_written) >= 3` löste bei
  **jedem Ramp-Schritt** einen Write aus — statt nur wenn sich die quantisierte
  Stufe ändert.

  Neu: Variable `_last_quantized_target_a` trackt den quantisierten Sollwert.
  Hysterese prüft jetzt `abs(target_a - self._last_quantized_target_a)`.
  Der gerampte Wert wird weiterhin an `set_max_charge_current()` übergeben
  (sanftes Rampen bleibt erhalten).

  Alt:
  ```python
  if abs(ramped - self._last_written_ramped_a) >= write_threshold:
      if self.victron.set_max_charge_current(ramped):
          self._last_written_ramped_a = self.state.charge_current_setpoint
  ```
  Neu:
  ```python
  if abs(target_a - self._last_quantized_target_a) >= write_threshold:
      self._last_quantized_target_a = target_a
      if self.victron.set_max_charge_current(ramped):
          self._last_written_ramped_a = self.state.charge_current_setpoint
  ```

- `controller.py` (Fix 2): `decide()`: `hours_left` im Optimal-Fenster
  änderte sich jede Minute minimal (z.B. 4.53h → 4.52h), was `required_a`
  langsam driften ließ. An Quantisierungsgrenzen (z.B. `round(12.4/5)*5=10`
  vs. `round(12.6/5)*5=15`) kippte die Stromstufe und löste einen Write aus.
  Dieses Kippen wiederholte sich alle 6–8 Minuten (beobachtetes Muster
  10A↔15A im Log vom 2026-06-13).

  `_smooth_required_a` (v3.0.9.26) war hier kontraproduktiv: er mischte
  Werte aus verschiedenen `hours_left`-Perioden und verzögerte das Kippen
  um 3 Zyklen, erzeugte es aber nicht weniger oft.

  Fix: `hours_left` auf 0.5h-Stufen quantisieren. Änderung nur 2× pro
  Stunde → `required_a` ist 30 Minuten stabil → kein Stufenwechsel durch
  Minutendrift. `_smooth_required_a` bleibt als Absicherung gegen
  SOC-Messrauschen erhalten.

  Alt:
  ```python
  hours_left = max((opt_end + 1.0) - h_now - minute_now / 60.0, 0.5)
  ```
  Neu:
  ```python
  hours_left_raw = max((opt_end + 1.0) - h_now - minute_now / 60.0, 0.5)
  hours_left = max(round(hours_left_raw * 2) / 2, 0.5)
  ```

  Erwartetes Verhalten: Stufenwechsel im Optimal-Fenster maximal 2× pro
  Stunde (bei echtem SOC-Fortschritt), statt alle 6–8 Minuten.

Changed:
- `version.py` neu eingeführt: `VERSION`-Konstante ausgelagert.
  `battery_manager.py` importiert `from version import VERSION`.
  Zukünftige Releases erfordern nur noch eine Änderung in `version.py` —
  `battery_manager.py` bleibt unverändert.

- `battery_manager.py`: Dateistruktur-Kommentar auf v3.0.10.6 aktualisiert
  (9 Module inkl. `version.py`).

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
