# Changelog — battery_manager.py
### Commits on May 22, 2026 
•	Add energy tracking, history buffer, fix startup Modbus write, details see changelog.md 73aa2fef0ff7b33a4afb18923c88939ac213dacb

**GitHub (master) → lokale Version**
Vergleich: 1.586 Zeilen (GitHub) vs. 1.894 Zeilen (lokal, inkl. heutiger Bugfix)

---

## 1. Neue Klasse: `HourlyHistory` (Dataclass)

**GitHub:** nicht vorhanden — vergangene Stunden wurden nicht gespeichert.

**Lokal:** neue Dataclass `HourlyHistory` mit folgenden Feldern:
```
date_iso, hour, pv_kwh, consumption_kwh, surplus_kwh,
action, charge_current_a, soc_start, soc_end, is_actual,
_raw_pv_total, _raw_cons_total,
_hour_start_pv_total, _hour_start_cons_total
```
Zweck: Speichert den tatsächlichen Stundenverlauf im Ringpuffer
`state.history_buffer` (max. 48 Einträge = 2 Tage).
Zugriff ausschließlich per Punkt-Notation (kein Dictionary-Zugriff).

---

## 2. Erweiterung `SystemState` — zwei neue Felder

**GitHub:** kein `history_buffer`, kein `pv_energy_today_kwh`, kein `load_energy_today_kwh`.

**Lokal (neu):**
```python
history_buffer: list = field(default_factory=list)  # [HourlyHistory, ...]
pv_energy_today_kwh: float = 0.0       # intern aufsummiert via EnergyAccumulator
load_energy_today_kwh: float = 0.0     # intern aufsummiert via EnergyAccumulator
```

---

## 3. Energie-Tagesintegration: `EnergyAccumulator`

**GitHub:** nicht vorhanden — Tagesenergie wurde nicht berechnet.

**Lokal:** neue Klasse `EnergyAccumulator` mit trapezförmiger Integration:
- Summiert PV- und Lastleistung sekündlich zu Tages-kWh-Werten
- Erkennt Tageswechsel (Mitternachts-Reset)
- Wird in der Hauptschleife in Schritt 2 aufgerufen:
  ```python
  energy.update(state.pv_power_w, state.load_power_w)
  state.pv_energy_today_kwh  = round(energy.pv_kwh,   3)
  state.load_energy_today_kwh = round(energy.load_kwh, 3)
  ```
- Ergebnis sichtbar im Dashboard als „Heute: X kWh" bei PV und Verbrauch

---

## 4. Energie-Basis über Neustarts hinweg: `_load_energy_base()` / `_save_persistent()`

**GitHub:** `_save_persistent()` speicherte nur:
`last_full_charge_date, days_since_full_charge, soc, charge_mode, charge_current_setpoint, timestamp`

**Lokal:** drei neue Felder in `state.json`:
```json
"energy_date":      "2026-05-22",
"energy_base_pv":   1.234,
"energy_base_load": 2.567
```
Neue Methode `_load_energy_base()` stellt beim Start die Energiebasis
des laufenden Tages wieder her, sodass „Heute: X kWh" nach einem Neustart
nicht bei 0 beginnt, sondern an der letzten bekannten Summe weiterzählt.
Bei Tageswechsel wird die Basis auf 0 zurückgesetzt.

---

## 5. Neuer Stundenplan: `build_schedule()` mit History-Integration

**GitHub:** `build_schedule()` verwendete ausschließlich Prognose-Daten für alle 24 Stunden.

**Lokal:** hybride Methode — vergangene Stunden werden mit echten Messwerten
aus `history_buffer` befüllt, zukünftige Stunden weiterhin mit `_simulate_hour()`:
- Vergangene Stunden: tatsächliche PV/Last/SOC-Werte aus `HourlyHistory`
- Lücken (Programm lief noch nicht): werden als `"action": "unknown"` markiert
- Aktuelle Stunde wird erst nach Minute 55 als „vergangen" behandelt
- Zukünftige Stunden: SOC-Simulation startet vom letzten tatsächlichen
  History-SOC (genauer als der aktuelle Messwert bei kurzzeitigen Sprüngen)
- Neues Feld `"is_past": true/false` in jedem Planeintrag für das Dashboard

---

## 6. Neuer `_update_history()` — stündlicher Ringpuffer

**GitHub:** nicht vorhanden.

**Lokal:** Methode `_update_history()` wird am Anfang von `run_cycle()` aufgerufen:
- Erstellt oder aktualisiert einen `HourlyHistory`-Eintrag pro Stunde
- Berechnet Stunden-Energie als Differenz der Tageskumulativen
  (Basis `_hour_start_pv_total` / `_hour_start_cons_total`)
- Schließt die vorherige Stunde beim Stundenwechsel korrekt ab
- Bereinigt Einträge älter als gestern (Rolling Window 48h)
- Verwendet Energie-Basis (`_energy_base_pv`) für korrekte
  Stundenberechnung nach Neustarts

---

## 7. Bugfix: Unnötiger Modbus-Write beim Programmstart

**GitHub:** `victron._last_written_a` startet immer als `None`.
Erster `set_max_charge_current()`-Aufruf schreibt immer, auch wenn
der Wert am Cerbo bereits korrekt ist.

**Lokal (heute hinzugefügt):** nach dem initialen `read_current_max_charge()`
wird `_last_written_a` sofort gesetzt:
```python
cur = victron.read_current_max_charge()
if cur is not None:
    logger.info(f"Aktueller MaxChargeCurrent laut Cerbo: {cur} A")
    state.charge_current_setpoint = cur
    victron._last_written_a = cur   # verhindert unnötigen Write beim Start
```

---

## 8. Erweiterung Persistenz-Speicherzyklus

**GitHub:** `_save_persistent()` alle 30 Minuten + bei Vollladung.

**Lokal:** Speicherintervall bleibt 30 Minuten (1800 s), aber der Aufruf
`_last_persistent_save` wird jetzt auch in `_save_persistent()` selbst
zurückgesetzt (nicht nur in `run_cycle()`), was doppelte Schreibvorgänge
bei Vollladungs-Events verhindert.

---

## 9. Dashboard: neue Anzeigeelemente

**GitHub:** kein „Heute: X kWh" bei PV und Verbrauch, kein History-Chart,
keine Unterscheidung vergangener/zukünftiger Stunden im Ladeplan.

**Lokal:**
- Karten „PV Leistung" und „Verbrauch" zeigen „Heute: X kWh"
- Ladeplan-Tabelle markiert vergangene Zeilen mit `tr.past` (gedimmt)
- Aktuelle Stunde als `tr.now` hervorgehoben
- SVG-Chart zeigt tatsächliche Stundenwerte für vergangene Stunden
  statt nur Prognosewerte
- Forecast-Quelle (VRM ★ / open_meteo / solcast) als Sub-Label
  unter PV-Prognose sichtbar
- Ladeplan-Badge `"discharging"` / `"unknown"` neu hinzugefügt

---

## 10. Hauptschleife `main()` — Reihenfolge und Energieschritt

**GitHub:** Schleife: `read_all → evcc → forecast → run_cycle`

**Lokal:** Schleife erweitert um Energieschritt:
```
read_all → energy.update → evcc → forecast → run_cycle
```
Außerdem: `forecast_source` wird beim Start und nach Prognose-Update
via `_forecast_source()` in den State geschrieben und im Dashboard
als Quelle angezeigt.

---

## Zusammenfassung der Dateigröße

| Version | Zeilen |
|---------|--------|
| GitHub master | 1.586 |
| Lokal (aktuell) | 1.894 |
| Differenz | **+308 Zeilen** |

Hauptursachen: `HourlyHistory`, `EnergyAccumulator`, `_update_history()`,
erweitertes `build_schedule()`, `_load_energy_base()`, Dashboard-Erweiterungen.

### 24.05.2026 (1)
 battery_manager v2.0.1 – Änderungsübersicht

Hier ist die vollständig korrigierte Datei zum Download.

## Zusammenfassung aller Änderungen gegenüber dem Original

| # | Fix | Datei/Funktion | Beschreibung |
|---|-----|----------------|--------------|
| 1 | **Atomic Write** | `ChargeController._save_persistent()` | `tempfile.mkstemp` + `os.fsync` + `os.replace` – verhindert korrupte `state.json` bei Stromausfall |
| 2 | **Min-Intervall** | `EnergyAccumulator.update()` | `MIN_UPDATE_INTERVAL_S = 1.0` – überspringt zu schnelle Updates, merkt aber Messwerte für nächstes gültiges Intervall |
| 3 | **Config-Validierung** | `validate_config()` (neu) | Fail-fast Prüfung von `capacity_kwh`, `max_charge_current`, `min_soc`/`max_soc`, `modbus.host`, `control_interval_seconds`, Koordinaten |
| A | **Bug A – Schedule** | `ChargeController.build_schedule()` | Klare Trennung Vergangenheit/Zukunft: Stunde ist entweder History (≥55 Min) oder Prognose (<55 Min), nie beides, nie keine |
| B | **Bug B – Hysterese** | `VictronModbus.set_max_charge_current()` | Interne Hysterese entfernt; Methode führt Write immer aus wenn aufgerufen; Entscheidung „ob geschrieben wird" liegt einzig beim `ChargeController` via `_last_written_ramped_a` |
| C | **days_since_full_charge** | days_since_full_charge immer aktuell aus Datum berechnen | Verhindert, dass der Wert nach einem langen Programmlauf nicht neuberechnet wird |

---

### 24.05.2026 (2)
battery_manager v2.0.2 – Änderungsübersicht

## Zusammenfassung aller Änderungen gegenüber v2.0.1

| # | Fix | Datei/Funktion | Beschreibung |
|---|-----|----------------|--------------|
| 1 | **Konsistente Aktion „Entladen"** | `ChargeController._simulate_hour()` | `night`-Variable und `if night:`-Block entfernt; neue Hilfsfunktion `_apply_deficit()` setzt `action = "discharging"` wenn `fc.net_kwh < 0`, sonst `"idle"` – gilt einheitlich für alle Stunden ohne Tag/Nacht-Unterscheidung |

### Hintergrund
Bisher wurde `"discharging"` nur für Stunden ab `night_start_hour` (Standard: 21 Uhr) gesetzt.
Stunden wie 19–20 Uhr zeigten `"idle"` obwohl der Akku ebenfalls entladen wurde (Verbrauch > PV).
Die Unterscheidung war rein durch die Uhrzeit gesteuert, nicht durch den tatsächlichen Energiefluss.

### Neues Verhalten
- `net_kwh < 0` → `"discharging"` (Verbrauch übersteigt PV, Akku gibt Energie ab)
- `net_kwh >= 0` → `"idle"` (kein Ladebefehl, aber kein Nettodefizit)
- Gilt für alle Stunden gleichwertig – keine separate Nachtlogik mehr
