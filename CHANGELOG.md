# Changelog — battery_manager.py

## [3.0.8.6] – 2026-05-31

### Fixed
- **`build_schedule()`: `floor_soc` berücksichtigt immer ESS MinimumSocLimit (Reg 2901)**
  `floor_soc` wurde bisher nur gesetzt wenn `evcc_discharge_locked == True`. Das Register 2901 (ESS MinimumSocLimit) ist jedoch die harte physikalische Untergrenze, die Victron in ESS State 11/12 durchsetzt — unabhängig davon, ob evcc aktiv ist oder nicht. Die Simulation zeigte daher Entladung unter den realen Hardware-Limit (z. B. 20 %), obwohl Victron den SOC dort physisch stoppt.
  → Fix: `floor_soc` wird jetzt immer aus `state.evcc_min_soc` (Reg 2901) gebildet, Fallback auf `bat.min_soc`. Die Simulation sinkt damit korrekt nur bis zur tatsächlichen Victron-Untergrenze.

- **`_simulate_hour()`: `_apply_deficit()` klemmt immer auf `floor_soc`**
  In `_apply_deficit()` wurde die Untergrenze nur angewendet wenn `soc >= floor_soc`. Fiel der simulierte SOC bereits unter `floor_soc` (z. B. 33 % < 35 %), wurde das Defizit ungeklemmt weiter abgezogen — der SOC lief in der Prognose ins Negative.
  → Fix: `max(floor_soc, new_soc)` wird jetzt in `_apply_deficit()` **immer** angewendet, unabhängig vom aktuellen SOC-Level. Das entspricht dem physikalischen Verhalten: Victron stoppt die Entladung am Hardware-Limit.

- **`_simulate_hour()`: Kein pauschales SOC-Einfrieren bei `soc_sim < min_soc` ohne PV**
  Der Block `if soc_sim < min_soc` mit `else`-Zweig (kein PV → SOC bleibt konstant, `action = "idle"`) frierte den simulierten SOC bei negativem Überschuss künstlich ein. Im Ladeplan blieb der SOC z. B. ab 03:00 bei 33.1 % stehen, obwohl weiterhin Last > PV vorhanden war und die reale Batterie weiter entlud (siehe Log: SOC 34.0 %).
  → Fix: Der `else`-Zweig wurde entfernt. Wenn `soc_sim < min_soc` und **kein** PV-Überschuss vorhanden ist, läuft die normale Defizit-Logik (`_apply_deficit`) weiter. Der SOC sinkt im Ladeplan physikalisch korrekt bis zur harten `floor_soc`-Grenze (Reg 2901). Nur bei **positivem** Überschuss wird weiterhin sofort geladen (`action = "charging"`).

- **`EvccMonitor.update()`: `state.evcc_min_soc` wird aus Reg 2901 geschrieben**
  `EvccMonitor.update()` las Reg 2901 in die Instanzvariable `self.evcc_min_soc`, schrieb den Wert jedoch nie in `state.evcc_min_soc`. `build_schedule()` las `state.evcc_min_soc` (immer `0.0`) und fiel daher auf `bat.min_soc` (35 %) zurück — statt auf den echten ESS MinimumSocLimit (20 %). Folge: Alle Fixes 1–3 wirkten sich nicht aus, da `floor_soc` effektiv weiterhin 35 % betrug.
  → Fix: Nach erfolgreichem Modbus-Lesen von Reg 2901 wird `self.state.evcc_min_soc = self.evcc_min_soc` gesetzt. Damit fließt der Victron-Hardware-Limit in den SystemState und wird von `build_schedule()` als `floor_soc` verwendet.

### Notes
- Der reale ESS State 11/12-Schutz in `decide()` (Echtzeit-Steuerung) bleibt unverändert. Die Änderungen betreffen ausschließlich die **Simulation** im Ladeplan (`build_schedule()` / `_simulate_hour()`), damit die Prognose mit der physikalischen Realität übereinstimmt.
- Die Kombination aus Fix 1 + Fix 2 + Fix 3 + Fix 4 stellt sicher, dass der Ladeplan bei Nacht mit negativem Überschuss korrekt bis zum Victron-Limit (Reg 2901) entlädt und dort stoppt — nicht früher (Fix 3) und nicht tiefer (Fix 1+2+4).

---

## [3.0.8.5] – 2026-05-30

### Fixed
- **`_get_dynamic_night_window()`: Debug-Logging entfernt**  
  Temporäres `[NIGHT_DBG]`-Logging aus v3.0.8.4d entfernt. Produktionsversion ohne Debug-Ausgaben.

---

## [3.0.8.4] – 2026-05-30

### Fixed
- **`VrmForecastManager.fetch()`: Timestamp-Filter auf heutigen Tag**  
  VRM liefert Einträge deren erste Timestamps (UTC-Mitternacht) auf lokale Stunden des **Vortags** fallen (z.B. UTC 22:00/23:00 = lokal gestern 22/23 Uhr). Diese überschrieben in `pv_by_hour`/`cons_by_hour` die echten heutigen Stunden 22/23. Folge: falsche Stunden-Zuordnung, `_get_dynamic_night_window()` berechnete ein verschobenes Fenster.  
  → Fix: Beide Aggregations-Schleifen filtern jetzt mit `if dt.date() != today: continue` — nur Einträge des aktuellen Kalendertags fließen in die Stunden-Maps ein.

### Added
- **Temporäres `[NIGHT_DBG]`-Logging** zur Diagnose der Stunden-Zuordnung in `_get_dynamic_night_window()` (wird in v3.0.8.5 wieder entfernt).

---

## [3.0.8.3] – 2026-05-30

### Fixed
- **`VrmForecastManager.fetch()`: Alle 24 Stunden in `HourlyForecast` aufbauen**  
  Die Schleife `for h in sorted(pv_by_hour.keys())` iterierte nur über Stunden mit PV-Daten (typisch 6–20 Uhr). Nachtstunden fehlten in `fc_list` → `night_consumption_kwh()` summierte nur einen Teil der Nacht → zu niedriger Wert (4.4 kWh statt korrekter ~4.1 kWh).  
  → Fix: `for h in range(24)` mit `pv_by_hour.get(h, 0.0)` — alle 24 Stunden werden aufgebaut, Nachtstunden mit `pv_kwh=0.0` und echtem Verbrauch aus `cons_by_hour`.  
  Nebeneffekt: `_get_dynamic_night_window()` findet Nachtgrenzen jetzt auch für Stunden ohne PV-Eintrag korrekt.

---

## [3.0.8.2] – 2026-05-30

### Fixed
- **`VrmForecastManager.fetch()`: Veraltete `_consumption_night_kwh`-Referenz im Log (`AttributeError`)**  
  In v3.0.8.1 wurde `self._consumption_night_kwh` aus `__init__()` und `fetch()` entfernt, aber die Log-Zeile am Ende von `fetch()` referenzierte das Attribut noch:  
  ```python
  + (f", Nachtverbrauch ~{self._consumption_night_kwh:.1f} kWh" if self._consumption_night_kwh else "")
  ```  
  → `AttributeError` beim ersten VRM-Fetch nach Programmstart.  
  → Fix: Log-Zeile auf `f"VRM-Prognose: {total_pv_kwh:.2f} kWh PV heute"` reduziert. Nachtverbrauch wird ohnehin separat über `[NIGHT_WINDOW]` geloggt.

---

## [3.0.8.1] – 2026-05-30

### Changed
- **`VrmForecastManager`: Nachtverbrauch aus Totals entfernt**  
  Vorher: Wenn VRM aktiv, wurde `_consumption_night_kwh` aus `data["totals"]["vrm_consumption_fc"]` berechnet — mit einer groben Gleichverteilung (`total_wh / 24 * night_hours`). Das ignorierte das dynamische Nachtfenster aus v3.0.8 vollständig und überschätzte den Nachtverbrauch systematisch (Nacht hat deutlich niedrigeren Verbrauch als der Tagesschnitt).  
  → Fix: `_consumption_night_kwh`-Attribut, dessen Berechnung in `fetch()` und die Methode `VrmForecastManager.night_consumption_kwh()` vollständig entfernt. `ForecastManager.night_consumption_kwh()` summiert jetzt immer aus den stündlichen Forecast-Daten mit dynamischem Fenster — konsistent und genauer.

### Removed
- `VrmForecastManager._consumption_night_kwh` (Attribut)
- `VrmForecastManager.night_consumption_kwh()` (Methode)
- Statische Nachtverbrauchsberechnung aus `VrmForecastManager.fetch()`
- VRM-Totals-Bevorzugung in `ForecastManager.night_consumption_kwh()`

---

## [3.0.8] – 2026-05-30

### Added
- **Dynamisches Nachtfenster in `ForecastManager`**  
  `night_start_hour` und `night_end_hour` waren bisher statische config-Werte (default 21/6). Der Nachtverbrauch wurde dadurch unabhängig von Jahreszeit und Wetterlage immer über ein fixes 9h-Fenster berechnet.  
  → Neu: `_get_dynamic_night_window()` bestimmt Start und Ende aus dem PV/Verbrauchs-Forecast:  
  - **Start (Abend):** erste Stunde ab 12 Uhr, in der PV-Ertrag < Hausverbrauch (Akku muss einspringen)  
  - **Ende (Morgen):** erste Stunde vor 12 Uhr, in der PV-Ertrag > Hausverbrauch (System wieder autark)  
  - **Clamps (nur im Code):** Start 16–21 Uhr, Ende 6–10 Uhr → Nacht zwischen 9h (Sommer) und 18h (Winter)  
  - **Fallback:** kein brauchbarer Forecast → `night_start_hour` / `night_end_hour` aus `config.yaml`; config-Werte werden ebenfalls geclampt  

- **`[NIGHT_WINDOW]`-Log in `ForecastManager`**  
  Nachtfenster wird geloggt — einmalig nach Programmstart und bei jeder Änderung, nicht jede Minute.  
  Beispiele:
  ```
  [NIGHT_WINDOW] 17:00–08:00 (15h, dynamisch)
  [NIGHT_WINDOW] 21:00–06:00 (9h, Fallback config)
  ```

### Notes
- `night_start_hour` / `night_end_hour` in `config.yaml` bleiben als Fallback erhalten — keine Migration nötig.  
- Clamp-Grenzen sind bewusst nicht konfigurierbar, um die config übersichtlich zu halten.  
- `_last_night_window = (-1, -1)` im `__init__` erzwingt den ersten Log-Eintrag nach Programmstart.

---

## [3.0.7] – 2026-05-30

### Fixed
- **ESS State 11/12: Priorität in `decide()` — Morgen-Fenster überschrieb State-11-Schutz**  
  Ab `morning_delay_start_hour` (06:00 Uhr) griff der Morgen-Notladungs-Block in `decide()` zuerst. Da SOC < `min_required` und kein PV-Überschuss vorhanden war, gab er `0 A / "idle"` zurück — der ESS-State-11/12-Block danach wurde nie erreicht. Folge: Der Controller rampte den Ladestrom von 50 A über 10 Minuten auf 0 A herunter, obwohl State 11 aktiv war und `max_a` gehalten werden sollte.  
  → Fix: ESS State 11/12 wird jetzt als **erster Block** in `decide()` geprüft, noch vor der Morgen-Notladung. State 11/12 ist ein Hardware-Eingriff durch Victron ESS mit höchster Priorität — kein anderer Block darf ihn überstimmen.  
  Bisherige Reihenfolge: `Morgen-Notladung → Notfall → (2b) ESS State 11/12`  
  Neue Reihenfolge: `(1) ESS State 11/12 → Morgen-Notladung → Notfall`

### Test
- State 11 aktiv um 05:59 (SOC 30%, MinSOC 35%): `50A | ESS State 11: Notladung/Entladesperre → max 50A` ✓  
- Ab 06:00 (Morgen-Fenster aktiv): weiterhin `50A | ESS State 11` — kein Abrampen auf 0 A ✓

---

## [3.0.6] – 2026-05-30

### Fixed
- **ESS State 11/12: Simulation zeigte "LADEN 50A" bei Nacht ohne PV**  
  In `_simulate_hour()` wurde bei `soc_sim < min_soc` (State 11/12 aktiv) immer `action = "charging"` und `current_a = max_a` gesetzt, auch wenn kein PV-Überschuss vorhanden war. Der Ladeplan zeigte dadurch in der Nacht "LADEN 50A" mit stagnierendem SOC — visuell irreführend, da State 11 nur die Entladung sperrt, aber nicht aus dem Netz lädt.  
  → Fix: Guard unterscheidet jetzt: `fc.net_kwh > 0` → "charging" (PV da), sonst → "idle" (kein PV, SOC bleibt konstant). Die realen History-Einträge (Vergangenheit) zeigen weiterhin die tatsächliche `decide()`-Aktion.

- **ESS State 11/12: Priorität in `decide()` korrigiert**  
  State 11/12 wurde in v3.0.4 noch über `ESS_DISCHARGE_BLOCKED_STATES = {11,12}` und `floor_soc` in `build_schedule()` abgebildet — verteilt auf mehrere Code-Stellen und schwer nachvollziehbar.  
  → Fix: Expliziter, dedizierter Block in `decide()` direkt nach der Morgen-Notladung. State 11/12 triggert sofort `max_a`, unabhängig von Tag/Nacht. `_simulate_hour()` fängt `soc_sim < min_soc` separat ab — keine indirekte Kopplung mehr über `floor_soc`.

- **`ESS_DISCHARGE_BLOCKED_STATES` auf `{11}` reduziert**  
  State 12 (Recharge / Zwangsladung aus Netz) wird jetzt direkt in `decide()` und `_simulate_hour()` behandelt, nicht über die Konstante. Semantisch korrekte Trennung: State 11 = Entladesperre, State 12 = aktive Netzladung.

- **`floor_soc` vereinfacht**  
  In v3.0.4: komplexe Kaskade (`if State 11/12 → min_soc`, `elif evcc → evcc_min_soc`, sonst `0.0`).  
  → Fix: `floor_soc` macht jetzt nur noch das, wofür es gedacht ist — evcc MinSoc-Sperre (Reg 2901). State-11/12-Schutz ist in die Simulation selbst verlagert.

- **Reg 2903 vollständig entfernt**  
  In v3.0.4 noch im Docstring und teilweise im Code erwähnt.  
  → Fix: Vollständig aus `SystemState`, `REGISTERS`, `read_all()`, `decide()`, `build_schedule()` und Doku entfernt. Im LFP-Modus "Optimiert ohne BatteryLife" wird Reg 2903 von Victron ignoriert.

- **Docstring/Header aktualisiert**  
  Alte State-Bezeichnungen ("BL Disabled") entfernt, aktuelle Victron-Terminologie für LFP-Modus eingeführt: State 10 = Self-consumption, State 11 = SOC below MinSOC, State 12 = Recharge.

### Test
- State 11 erreicht bei SOC 31% (VRM MinSoc 30%, config min_soc 35%).  
  Verhalten bestätigt: Entladen gesperrt, SOC bleibt konstant, `MaxChargeCurrent = 50A` wird auf Bus geschrieben (wirkungslos ohne PV, aber harmlos). Kein ungewolltes Netzladen.

---
## [3.0.3] – 2026-05-28

### Fixed
- **Morgen-Verzögerung: Lücke zwischen `morning_delay_end_hour` und `opt_start`**  
  Wenn `morning_delay_end_hour` (z.B. 10 Uhr) vor dem Optimal-Fenster-Start `opt_start` (z.B. 11 Uhr) lag, entstand eine Lücke von einer Stunde. In dieser Lücke fiel die Logik in den normalen PV-Überschuss-Block und lud sofort — obwohl das Optimal-Fenster noch ausreichend PV versprach.  
  → Fix: `effective_morn_e = max(morn_e, opt_start)` schließt die Lücke. Der Morgen-Verzögerungs-Block reicht nun garantiert bis zum Optimal-Fenster.

- **Morgen-Verzögerung: falsche Guard-Bedingung in `decide()`**  
  Die Bedingung `soc > min_required + 5` war semantisch falsch: sie sollte verhindern, dass bei kritisch niedrigem SOC gewartet wird. Tatsächlich verhinderte sie aber zu oft das Warten, weil `min_required` (Notfall-SOC, z.B. 25%) nicht das Ziel-SOC (z.B. 66%) ist.  
  → Fix: `soc >= min_required` — warte solange der SOC nicht im Notfallbereich liegt.

- **Konsistenz zwischen `decide()` und `_simulate_hour()`**  
  `_simulate_hour()` (Ladeplan) hatte bereits den `effective_morn_e`-Fix, `decide()` (echte Steuerung) aber nicht. Das führte zu divergierenden Anzeigen: Dashboard-Text sagte "warte", Ladeplan zeigte "LADEN".  
  → Beide Methoden verwenden nun identische Logik.

- **evcc MinSoc im Ladeplan**

  Simulation zeigte SOC bis 20%, obwohl Reg 2901 auf z.B. 60% sperrt	
   floor_soc Parameter in _simulate_hour(), build_schedule() ermittelt effektiven MinSoc

---


## [3.0.2] – 2026-05-27

### Fixed
- **Balancing-Timer: Mitternachts-Reset**  
  Der Cellbalancing-Timer (`_balancing_hold_until`) wurde bisher nur bei SOC-Abfall unter 98% zurückgesetzt. Bei stundenlangem Pendeln um 97,5–98,5% (Wolken, Lastspitzen) startete der Timer immer wieder von vorne — die geforderte Haltezeit wurde nie erreicht.  
  → Fix: Hart-Reset um Mitternacht (`_balancing_reset_date`), unabhängig von Vorgeschichte.

- **Balancing-Timer: kein sofortiger Kill bei kurzen SOC-Unterschreitungen**  
  Ein kurzer SOC-Abfall unter 98% (z.B. 2 Minuten hohe Last) setzte den Timer sofort auf 0.  
  → Fix: Timer wird nur verworfen wenn SOC deutlich unter `max_soc - hyst` fällt (z.B. < 96%).

---

## [3.0.1] – 2026-05-27

### Fixed
- **Balancing-Timer: Guard gegen Mehrfach-Start nach Auto-Reset**  
  Nach dem Auto-Reset (`_soc_98_reached_at = None`) wurde `_balancing_hold_until` beim nächsten Zyklus neu gesetzt, obwohl der alte Timer noch lief.  
  → Fix: Guard `if self._balancing_hold_until <= time.monotonic()` statt `== 0.0`.

---
### 27.05.2026 — battery_manager v3.0.1

## Cellbalancing-Haltezeit bei SOC ≥ max_soc

### 27.05.2026 (2) — battery_manager v3.0.1

## ESS-Modus Fix: SOC-Prognose bei negativem PV-Überschuss

**Anforderung:** Im deutschen ESS-Modus (kein Netzbezug zum Laden) entlädt sich
der Akku bei negativem Überschuss (Last > PV) auch dann, wenn ein Ladestrom
> 0 A gesetzt ist. Die SOC-Prognose in `_simulate_hour()` ignorierte diese
physikalische Realität und zeigte einen stabilen SOC an, obwohl der Akku
sich tatsächlich entlud.


**Änderungen:**

| Datei | Stelle | Änderung |
|-------|--------|----------|
| `battery_manager.py` | `_simulate_hour()` — Notfall-SOC | `fc.net_kwh > 0` → laden mit `min(fc.net_kwh, max_charge_kwh)`; `fc.net_kwh <= 0` → Entladung mit `max(0.0, soc_sim - deficit)` |
| `battery_manager.py` | `_simulate_hour()` — needs_full | `fc.net_kwh > 0` → laden; `fc.net_kwh <= 0` → Entladung mit `max(min_soc, soc_sim - deficit)`. `action` bleibt `"full_charge"`, `current_a` bleibt `max_a` (Setpoint wird geschrieben, aber Netz lädt nicht) |
| `battery_manager.py` | `_simulate_hour()` — Morgen-Notladung | Gleiche Struktur: nur bei Überschuss laden, sonst Entladung mit `max(min_soc, ...)` |
| `battery_manager.py` | `_simulate_hour()` — max_charge_kwh | Variable wurde vor den Notfall-SOC-Block verschoben (war nach dem Block definiert → `NameError`) |

**Semantik:**
- `action = "charging"` / `"full_charge"` beschreibt den **Sollwert** (DVCC Setpoint)
- Die SOC-Prognose reflektiert die **physikalische Realität** (ESS-Modus = kein Netzbezug)
- Bei negativem Überschuss sinkt der SOC trotz gesetztem Ladestrom

**Neues Verhalten (nach dem Fix):**
```
Uhr    PV kWh    Last kWh    Ueberschuss    Aktion       Strom    SOC %
20:00  0.213     0.775       -0.56          Entladen     -        94.0%
21:00  0.000     0.627       -0.63          Vollladung   50A      93.2%  ← korrekt
22:00  0.000     0.491       -0.49          Vollladung   50A      92.4%  ← korrekt
23:00  0.000     0.326       -0.33          Vollladung   50A      91.8%  ← korrekt
```

**Unterschiedliche Untergrenzen:**
- Notfall-SOC: `max(0.0, ...)` — Akku darf in der Prognose unter `min_soc` fallen (echter Notfall)
- Alle anderen: `max(min_soc, ...)` — normale Betriebsgrenze

---


**Anforderung:** Wenn SOC ≥ 98% erreicht wird, darf der Ladestrom erst nach
mindestens 5 Stunden auf 0 reduziert werden, damit der BMS ein vollständiges
Cellbalancing durchführen kann.

**Änderungen:**

| Datei | Stelle | Änderung |
|-------|--------|----------|
| `battery_manager.py` | `ChargeController.__init__()` | Neues Attribut `_balancing_hold_until: float = 0.0` (monotonic timestamp) |
| `battery_manager.py` | `ChargeController.run_cycle()` | `_balancing_hold_until` wird gesetzt sobald SOC ≥ 98% erstmals erreicht; Reset wenn SOC wieder unter 98% fällt |
| `battery_manager.py` | `ChargeController.decide()` | Im „Ziel erreicht"-Block: solange `_balancing_hold_until` noch nicht abgelaufen ist, wird `trickle_current` statt `0 A` zurückgegeben |
| `config.yaml` | `battery:` | Neues optionales Feld `balancing_hold_hours: 5` (rückwärtskompatibel, Default 5) |

**Verhalten:**
- Beim ersten Erreichen von SOC ≥ 98% startet ein 5-Stunden-Timer
- Während dieser Zeit gibt `decide()` `trickle_current` (5 A) zurück statt `0 A`
- Im Log erscheint: `[TRICKLE] 5A | Cellbalancing: SOC 98.x% >= 98%, halte 5A noch NNN min`
- Nach Ablauf der Haltezeit: normaler Übergang zu `idle / 0 A`
- Fällt der SOC vor Ablauf unter 98%, wird der Timer zurückgesetzt (neuer Ladevorgang)
- Der bestehende Auto-Reset von `days_since_full_charge` nach 1 Stunde läuft unverändert parallel

**Rückwärtskompatibilität:** `balancing_hold_hours` ist optional – fehlt das Feld
in einer alten `config.yaml`, greift automatisch der Default von 5 Stunden via
`self.bat.get("balancing_hold_hours", 5)`.

---

### Commits on May 22, 2026
• Add energy tracking, history buffer, fix startup Modbus write, details see changelog.md 73aa2fef0ff7b33a4afb18923c88939ac213dacb

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
history_buffer: list = field(default_factory=list) # [HourlyHistory, ...]
pv_energy_today_kwh: float = 0.0 # intern aufsummiert via EnergyAccumulator
load_energy_today_kwh: float = 0.0 # intern aufsummiert via EnergyAccumulator
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
  state.pv_energy_today_kwh = round(energy.pv_kwh, 3)
  state.load_energy_today_kwh = round(energy.load_kwh, 3)
  ```
- Ergebnis sichtbar im Dashboard als „Heute: X kWh" bei PV und Verbrauch

---

## 4. Energie-Basis über Neustarts hinweg: `_load_energy_base()` / `_save_persistent()`

**GitHub:** `_save_persistent()` speicherte nur:
`last_full_charge_date, days_since_full_charge, soc, charge_mode, charge_current_setpoint, timestamp`

**Lokal:** drei neue Felder in `state.json`:
```json
"energy_date": "2026-05-22",
"energy_base_pv": 1.234,
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
    victron._last_written_a = cur # verhindert unnötigen Write beim Start
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

---

### 25.05.2026
battery_manager v2.0.3 – Änderungsübersicht

## Zusammenfassung aller Änderungen gegenüber v2.0.2

| # | Fix | Datei/Funktion | Beschreibung |
|---|-----|----------------|--------------|
| 1 | **Bugfix SOC-Ping-Pong nach Ziel-Erreichen** | `ChargeController._simulate_hour()` | SOC wird nach Erreichen des Ladeziels auf die Hysterese-Schwelle geklemmt, wenn kein echtes Netto-Deficit vorliegt (`net_kwh >= 0`) |

### Hintergrund
Nach Erreichen des Ziel-SOC (z.B. 78.3% bei Ziel 80%, Hysterese 2%) konnte ein minimales
Netto-Deficit (z.B. −0.055 kWh um 18:00 Uhr) den simulierten SOC knapp unter die
Hysterese-Schwelle (78%) drücken. Die Folgestunde wertete das als „Ziel nicht erreicht"
und erzeugte einen erneuten LADEN-Eintrag (z.B. 19:00 Uhr mit 6 A), obwohl das Ziel
faktisch gehalten wird. In der Realität würde das Gerät bei einem solch minimalen Abfall
sofort wieder einschalten – der Ping-Pong-Effekt war ein reines Simulations-Artefakt.

### Neues Verhalten
Nach dem Block „Ziel erreicht" in `_simulate_hour()`:
```python
if fc.net_kwh >= 0:
    soc_sim = max(soc_sim, dyn_target - hyst)
```
- `net_kwh < 0` (echtes Deficit) → SOC darf weiter sinken (Akku entlädt sich)
- `net_kwh >= 0` (Überschuss oder ausgeglichen) → SOC wird auf Hysterese-Schwelle gehalten

### Sichtbarer Effekt im Ladeplan
Vorher: SOC sank zwischen 12:00 und 19:00 trotz PV-Überschuss schrittweise ab,
und um 19:00 wurde fälschlicherweise wieder LADEN mit 6 A angezeigt.
Nachher: SOC bleibt nach Ziel-Erreichen stabil; LADEN taucht erst dann wieder auf,
wenn ein echtes Netto-Deficit den SOC dauerhaft unter die Hysterese-Schwelle drückt.

---

### 25.05.2026 (2)
battery_manager v2.0.4 – Änderungsübersicht

## Zusammenfassung aller Änderungen gegenüber v2.0.3

| # | Fix | Datei/Funktion | Beschreibung |
|---|-----|----------------|--------------|
| 1 | **Verbesserter Ping-Pong-Schutz** | `ChargeController._simulate_hour()` | SOC-Klemmung nach Ziel-Erreichen toleriert jetzt Deficits bis zur Größe der Hysterese-Energie (`hyst_kwh = hyst / 100 * cap`) statt nur `net_kwh >= 0` |

### Hintergrund
v2.0.3 klemmt den SOC nur bei `net_kwh >= 0`. Ein minimales Deficit (z.B. −0.055 kWh)
würde trotzdem nicht geklemmt und könnte den SOC unter die Hysterese-Schwelle drücken.
Der Ping-Pong-Effekt war damit unter Grenzwertbedingungen noch möglich.

### Neues Verhalten
```python
hyst_kwh = hyst / 100.0 * cap # z.B. 2% von 14 kWh = 0.28 kWh
if fc.net_kwh >= -hyst_kwh:
    soc_sim = max(soc_sim, dyn_target - hyst)
```
- Deficits kleiner als `hyst_kwh` (z.B. −0.06 kWh < −0.28 kWh) → SOC bleibt geklemmt
- Deficits größer als `hyst_kwh` (z.B. −0.47 kWh ab 20:00) → SOC darf sinken
- Entspricht dem realen Hysterese-Verhalten von DVCC exakter als die `>= 0`-Prüfung

---

### 25.05.2026 (3)
battery_manager v2.0.5 – Änderungsübersicht

## Zusammenfassung aller Änderungen gegenüber v2.0.4

| # | Fix | Datei/Funktion | Beschreibung |
|---|-----|----------------|--------------|
| 1 | **Schwerer Bug: Modbus-Write jede Minute – Decision-Logik** | `ChargeController.run_cycle()` | Flags `hysterese_abgelaufen` und `wert_geaendert` entfernt; Write findet jetzt ausschließlich statt wenn `abs(ramped - _last_written_ramped_a) >= 1.0` |
| 2 | **Shadow-Variable im Modbus-Layer** | `VictronModbus.set_max_charge_current()` | Zweite, unabhängige Schutzschicht: identischer Wert wird nie ein zweites Mal auf den Bus geschrieben, unabhängig von der Decision-Logik |

### Ursache Fix 1 (Decision-Logik)
In v2.0.1 wurde die interne Hysterese aus `set_max_charge_current()` entfernt
(Bug B) und die Entscheidungshoheit an `run_cycle()` übertragen. Dabei wurde
`hysterese_abgelaufen` eingeführt:

```python
hysterese_abgelaufen = (now - self._last_decision_ts) < 1.0 # soeben neu entschieden
if hysterese_abgelaufen or wert_geaendert:
    victron.set_max_charge_current(ramped)
```

`hysterese_abgelaufen` ist `True` direkt nach jeder neuen `decide()`-Entscheidung.
Da `decide()` alle `min_charge_duration_minutes` aufgerufen wird, löste das Flag
regelmäßig einen Write aus — auch wenn der Wert unverändert `0 A → 0 A` war.
`wert_geaendert` schützte nur *zwischen* Entscheidungen, aber nie *beim*
Entscheidungszeitpunkt selbst.

### Fix 1
Beide Flags entfernt. Einziges Schreibkriterium in `run_cycle()`:

```python
if abs(ramped - self._last_written_ramped_a) >= 1.0:
    victron.set_max_charge_current(ramped)
```

### Fix 2 (Shadow-Variable, unabhängig von Fix 1)
`set_max_charge_current()` prüft jetzt selbst vor jedem Write:

```python
if self._last_written_a is not None and self._last_written_a == current_a:
    self.state.charge_current_setpoint = current_a
    return True # Wert stimmt bereits, kein Write nötig
```

`_last_written_a` wird beim Programmstart via `read_current_max_charge()` mit dem
aktuellen Cerbo-Wert vorbelegt. Damit ist es strukturell unmöglich, denselben Wert
zweimal auf den Modbus-Bus zu schreiben — unabhängig davon was die Decision-Logik
übergibt. Beide Schutzschichten sind voneinander unabhängig und sichern sich gegenseitig ab.

---

### 25.05.2026 (4)
battery_manager v2.0.6 – Änderungsübersicht

## Zusammenfassung aller Änderungen gegenüber v2.0.5

| # | Fix | Datei/Funktion | Beschreibung |
|---|-----|----------------|--------------|
| 1 | **Bugfix: Ladestrom zeigt 0 A nach Neustart** | `main()` | `controller._ramp_current` und `controller._last_written_ramped_a` werden nach Controller-Initialisierung mit dem vom Cerbo gelesenen Ist-Wert vorbelegt |

### Ursache
Nach einem Neustart wird der aktuelle `MaxChargeCurrent` des Cerbo (z.B. 50 A) korrekt
in `victron._last_written_a` und `state.charge_current_setpoint` gesetzt.
Der `ChargeController` wird jedoch danach neu erzeugt und initialisiert
`_ramp_current = 0.0` sowie `_last_written_ramped_a = 0.0` — ohne Kenntnis des
echten Cerbo-Werts.

Beim ersten `run_cycle()`:
- `decide()` liefert z.B. `target_a = 50` (PV-Überschuss)
- `_ramp(50)` → `_ramp_current = min(0 + 5, 50) = 5` (Rampe startet bei 0!)
- `abs(5 - 0) >= 1` → Write `5 A` → Cerbo wird fälschlicherweise auf 5 A gedrosselt
- Dashboard zeigt `5 A` statt `50 A`

Bei PAUSE-Entscheidung (`target_a = 0`):
- `_ramp(0) = 0`, Shadow `_last_written_a = 50 ≠ 0` → Write `0 A`
- Dashboard zeigt `0 A` obwohl Cerbo noch mit 50 A lädt

### Fix
Nach der Controller-Initialisierung in `main()`:
```python
if cur is not None:
    controller._ramp_current = cur
    controller._last_written_ramped_a = cur
```
Alle drei Startwert-Variablen (`victron._last_written_a`, `controller._ramp_current`,
`controller._last_written_ramped_a`) zeigen jetzt auf den gleichen Cerbo-Ist-Wert.
Die Rampe setzt nahtlos am echten Ausgangspunkt an, kein falscher Write beim ersten Zyklus.

---

### 25.05.2026 (5)
battery_manager v2.0.7 – Änderungsübersicht

## Zusammenfassung aller Änderungen gegenüber v2.0.6

| # | Fix | Datei/Funktion | Beschreibung |
|---|-----|----------------|--------------|
| 1 | **Bugfix: SOC-Projektion überschreitet Ladeziel** | `ChargeController._simulate_hour()` | Im PV-Überschuss-Block wird `soc_sim` jetzt auf `dyn_target` gedeckelt statt auf `max_soc` |

### Ursache
Im PV-Überschuss-Block wurde der SOC nach dem Laden auf `max_soc` (98%) gedeckelt:

```python
soc_sim = min(max_soc, soc_sim + (charge_kwh / cap) * 100)
```

Da `max_soc = 98%` und das normale Ladeziel `dyn_target = 80%` ist, wurde der
simulierte SOC weit über das Ziel hinaus projiziert. Beispiel aus dem Screenshot:
SOC 75% + 2.4 kWh Ladung (50 A × 48 V) = ~92% — obwohl die Ladeentscheidung
bei 80% stoppt. Die Folgestunden zeigten deshalb korrekt PAUSE, aber mit einem
unrealistischen Ausgangs-SOC von 91%.

### Fix
```python
soc_sim = min(dyn_target, soc_sim + (charge_kwh / cap) * 100)
```
Der SOC wird beim normalen PV-Überschuss-Laden auf `dyn_target` gedeckelt.
Das entspricht dem realen Verhalten: Die Ladesteuerung stoppt bei Ziel-Erreichen,
überschüssige PV-Energie geht ins Netz.

Hinweis: `full_charge`- und `trickle`-Blöcke bleiben auf `max_soc` gedeckelt,
da dort das Laden explizit bis `max_soc` (Zellbalancing) vorgesehen ist.


---

### 25.05.2026 (6)
battery_manager v3.0.0 – Änderungsübersicht

## Zusammenfassung aller Änderungen gegenüber v2.0.7

| # | Feature | Datei/Funktion | Beschreibung |
|---|---------|----------------|--------------|
| 1 | **Dynamisches target_soc** | `ChargeController._calculate_target_soc()` | `target_soc` ist nicht mehr fest `target_soc_normal` (80%), sondern wird für jeden Tag individuell berechnet: `max(min_soc, emergency_charge_soc) + (night_consumption_kWh / capacity_kWh) × 100%`, capped auf 98% |
| 2 | **Volllade-Zwang alle 10 Tage** | `ChargeController._calculate_target_soc()` | Wenn `days_since_full_charge ≥ 10` → `target_soc = 98%` (Vollladung für Zellbalancing) |
| 3 | **Morgen-Notladung** | `ChargeController.decide()` | Bei Sonnenaufgang im Morgenfenster: Wenn `SOC < min(emergency_charge_soc, min_soc)` → sofort laden mit `max_charge_current` (kein Warten auf `morning_delay_end_hour`). Ladeplan wird erst erstellt, wenn SOC ≥ Minimum erreicht ist |
| 4 | **Adaptive Ladezeitfenster** | `ChargeController.decide()` / `_simulate_hour()` | Je niedriger der PV-Überschuss, desto früher muss mit Laden begonnen werden – auch schon vor `morning_delay_end_hour` bei geringem Überschuss. Bei genug PV im optimalen Fenster → Warten, sonst frühes Laden |
| 5 | **Sonnenhöchststand-Optimierung** | `ChargeController._get_optimal_charge_window()` / `decide()` | Hauptladefenster um 13:00 ± `solar_noon_offset_hours` (Default: 2h = 11:00–15:00). Im optimalen Fenster bei genug PV → Ladestrom auf `reduced_charge_current_a` (Default: 20 A) reduziert, um das 4h-Fenster besser auszunutzen statt frühzeitig auf Ziel-SOC zu kommen |
| 6 | **Auto-Reset Vollladung** | `ChargeController.run_cycle()` | Monitoring: Wenn `SOC ≥ 98%` für mindestens 1 Stunde erreicht wurde → `days_since_full_charge` in `state.json` sofort auf `0` gesetzt, auch wenn kein expliziter `full_charge_cycle` eingeplant war |
| 7 | **Rückwärtskompatibilität Config** | `config.yaml` / `validate_config()` | Neue optionale Felder `solar_noon_offset_hours` (Default: 2) und `reduced_charge_current_a` (Default: 20) werden mit sinnvollen Defaults belegt. Alte `config.yaml` ohne diese Felder funktioniert ohne Anpassung |

---

## 1. Dynamisches target_soc (statt fester 80%)

**v2.0.7:** `target_soc` war konstant `target_soc_normal` (80%).

**v3.0.0:** Neue Methode `_calculate_target_soc()` berechnet täglich:

```python
def _calculate_target_soc(self) -> float:
    min_required = max(self.bat["min_soc"], self.cc.get("emergency_charge_soc", 25))
    night_cons = self.forecast.night_consumption_kwh()
    capacity = self.bat["capacity_kwh"]

    if self.state.days_since_full_charge >= self.bat.get("full_charge_interval_days", 10):
        return 98.0  # Vollladung fällig

    target = min_required + (night_cons / capacity) * 100.0
    target = min(target, 98.0)
    return max(target, min_required)
```

- `night_consumption_kWh`: Erwarteter Verbrauch von Sonnenuntergang bis Sonnenaufgang (aus VRM-Prognose oder historischen Werten)
- Ergebnis liegt immer zwischen `min_soc`/`emergency_charge_soc` und `target_soc_full` (98%)

---

## 2. Morgen-Notladung (Emergency Charge)

**v2.0.7:** Ladung startete frühestens ab `morning_delay_start_hour`, auch wenn der SOC kritisch niedrig war.

**v3.0.0:** Bei Sonnenaufgang im Morgenfenster (6:00–10:00 Uhr):
1. Prüfe aktuellen SOC
2. Wenn `SOC < min(emergency_charge_soc, min_soc)`:
   - Sofort laden mit `max_charge_current` (kein Warten auf `morning_delay_end_hour`)
   - Ladeplan wird erst erstellt, wenn SOC ≥ Minimum erreicht ist
3. Danach: Berechne dynamisches `target_soc` und erstelle normalen Tages-Ladeplan

---

## 3. Adaptive Ladezeitfenster & Sonnenhöchststand-Optimierung

**v2.0.7:** Ladeplan war primär durch `morning_delay_start_hour`/`morning_delay_end_hour` und PV-Prognose gesteuert.

**v3.0.0:** Intelligente Fenster-Logik mit `_get_optimal_charge_window()`:

| PV-Überschuss | Verhalten |
|---------------|-----------|
| Gering | Laden beginnt so früh wie nötig – auch vor `morning_delay_end_hour` (z.B. 8:00 Uhr), um das Ziel zu erreichen |
| Mittel | Ladefenster verschiebt sich Richtung Sonnenhöchststand (11:00–15:00) |
| Hoch | Hauptladung konzentriert sich um Sonnenhöchststand ±2h mit reduziertem Strom (z.B. 20 A statt 50 A) |

Um das 4h-Fenster (Sonnenhöchststand ±2h) besser auszunutzen, wird bei ausreichendem Überschuss der Ladestrom reduziert. Das verhindert, dass die Ladung zu früh beendet ist und PV-Energie später ins Netz geht.

---

## 4. Auto-Reset: days_since_full_charge

**v2.0.7:** `days_since_full_charge` wurde nur bei einem explizit geplanten `full_charge_cycle` zurückgesetzt.

**v3.0.0:** Monitoring-Regel in `run_cycle()`:
- Wenn `SOC ≥ 98%` für mindestens eine Stunde erreicht wurde
- → `days_since_full_charge` wird sofort in `state.json` auf `0` gesetzt
- → Gilt auch, wenn kein `full_charge_cycle` explizit eingeplant war (z.B. durch unerwartet hohen PV-Überschuss)
- → `_soc_98_reached_at` (Timestamp) wird zurückgesetzt wenn SOC wieder unter 98% fällt

---

## 5. Rückwärtskompatibilität

**v3.0.0:** Neue optionale Konfigurationsfelder werden mit sinnvollen Defaults belegt:

```yaml
charging:
  # NEU (optional, Default: 2)
  solar_noon_offset_hours: 2        # ± Stunden um 13:00 (Sonnenhöchststand)
  # NEU (optional, Default: 20)
  reduced_charge_current_a: 20      # Reduzierter Strom im Optimal-Fenster
```

Alte `config.yaml` ohne diese Felder funktioniert ohne Anpassung – die Defaults greifen automatisch via `dict.get(key, default)`.

**Validierung** in `validate_config()`:
- `solar_noon_offset_hours ≥ 0`
- `reduced_charge_current_a ≤ max_charge_current`
- `reduced_charge_current_a ≥ 0`

---

## 6. Bugfix: Nachtverbrauch-Anzeige 0.0 kWh (v3.0.0 final)

**Problem:** `state.forecast_consumption_night_kwh` wurde in v3.0.0 initial mit `0.0` gesetzt, aber nie aktualisiert. Das Dashboard zeigte daher immer `0.0 kWh` für den Nachtverbrauch, obwohl die interne Berechnung korrekt war (VRM lieferte z.B. `~4.7 kWh`).

**Ursache:** In der ursprünglichen v2.0.7 wurde `forecast_consumption_night_kwh` in `decide()` gesetzt:
```python
self.state.forecast_consumption_night_kwh = round(night_cons, 2)
```
In v3.0.0 wurde `decide()` komplett neu geschrieben, aber diese Zeile wurde vergessen.

**Fix:** `state.forecast_consumption_night_kwh` wird jetzt in `_calculate_target_soc()` gesetzt – das ist robuster, weil diese Methode sowohl von `decide()` als auch von `_simulate_hour()` (in `build_schedule()`) aufgerufen wird:
```python
def _calculate_target_soc(self) -> float:
    night_cons = self.forecast.night_consumption_kwh()
    self.state.forecast_consumption_night_kwh = round(night_cons, 2)  # <-- Fix
    ...
```

**Ergebnis:** Dashboard zeigt jetzt den korrekten Nachtverbrauch (z.B. `4.7 kWh`) und das Ziel-SOC wird korrekt berechnet (`25% + (4.7/14)*100 = 58.6%` statt fälschlicher `48%`).

---

## 7. Code-Review Fixes (v3.0.0 final)

### Bug 1: `proj_eve` in `_simulate_hour()` nicht definiert

**Problem:** In `_simulate_hour()` (Morgen-Fenster-Block, Stunde vor Optimal-Fenster) wurde die lokale Variable `proj_eve` aus `decide()` referenziert. Da `proj_eve` in `_simulate_hour()` nicht existiert, würde dies bei Erreichen dieses Code-Pfads einen `NameError` zur Laufzeit verursachen.

**Fix:** Bedingung vereinfacht – statt `if proj_eve >= dyn_target or soc_sim > min_required + 5:` jetzt nur `if soc_sim > min_required + 5:`. Die `proj_eve`-Variable bleibt korrekt in `decide()` als lokale Berechnung erhalten.

### Bug 2: State-Feld Überschreibung durch `_calculate_target_soc()`

**Problem:** `_calculate_target_soc()` setzte `state.forecast_consumption_night_kwh` als Seiteneffekt. Da `_simulate_hour()` (in `build_schedule()`) für jede der 24 Stunden `_calculate_target_soc()` aufruft, wurde das State-Feld 24× pro Zyklus mit dem gleichen Wert überschrieben – funktional korrekt, aber Design-Smell.

**Fix:** State-Update in neue Methode `_update_night_consumption_display()` extrahiert:
```python
def _update_night_consumption_display(self) -> None:
    night_cons = self.forecast.night_consumption_kwh()
    self.state.forecast_consumption_night_kwh = round(night_cons, 2)
```
Wird einmal pro Zyklus in `decide()` aufgerufen. `_calculate_target_soc()` hat jetzt keine Seiteneffekte mehr.

### Bug 3: Versionsnummer im HTML-Header

**Problem:** Der HTML-Header des Dashboards zeigte noch `v2.0.7`, während `main()` bereits `v3.0.0` loggte.

**Fix:** HTML-Title auf `Solar Batterie Manager v3.0.0` aktualisiert.

---

## Zusammenfassung der Dateigröße

| Version | Zeilen |
|---------|--------|
| v2.0.7 (GitHub) | ~1.998 |
| v3.0.0 final | ~2.090 |
| Differenz | **~+92 Zeilen** |

Hauptursachen: `_calculate_target_soc()`, `_get_optimal_charge_window()`, Morgen-Notladung, adaptive Fensterlogik, Auto-Reset-Vollladung, Nachtverbrauch-Display-Fix, Code-Review-Fixes.
