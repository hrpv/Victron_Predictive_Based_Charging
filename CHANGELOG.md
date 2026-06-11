# Changelog — battery_manager.py

## [3.0.9.24] – 2026-06-10

### Changed

- **`_simulate_hour()`: `current_a = min_charge_current` statt `0.0` bei `action=idle` und SOC > `floor_soc`**

  Bisher lieferte `_simulate_hour()` für idle-Stunden `current_a = 0.0` zurück, obwohl
  Reg. 2705 physikalisch immer auf mindestens `min_charge_current` (z.B. 3 A) steht
  (`write_charge_current()` clampt entsprechend). Der Strom fließt also tatsächlich.

  Neue Logik in `_apply_deficit()`:
  - `soc > floor_soc` → `current_a = min_charge_a`, SOC steigt netto um
    `(trickle_kwh − deficit_kwh) / cap * 100` pro Stunde (bei 3 A, 48 V, 100 Ah
    und typischem nächtlichem Verbrauch ca. +0.5–1 %/h).
  - `soc ≤ floor_soc` → `current_a = 0.0`, SOC eingefroren (Entladesperre
    Victron ESS State 11/12, Verbraucher aus Netz). Ausnahme unverändert.

  Folge für Dashboard-Tabelle: idle-Stunden zeigen jetzt `+3 A` statt `0 A` und
  einen leicht steigenden projizierten SOC anstelle eines konstanten Wertes.

- **`_apply_deficit()` gibt 3-Tupel `(action, current_a, new_soc)` zurück**
  (vorher `(action, new_soc)`). Alle internen Aufrufe angepasst.

- Versionsstring in GUI-Titel, h1-Überschrift, `logger.info` und Datei-Header
  auf `3.0.9.24` aktualisiert.

---

## [3.0.9.23] – 2026-06-10

### Fixed

- **`build_schedule()`: `planned_current_a` für idle/PAUSE-Stunden mit positivem Überschuss falsch (zu hoch)**

  In v3.0.9.22 wurde bei `action = idle` (Dashboard: PAUSE) der Überschussstrom ungecappt angezeigt, z.B. `+28 A` statt physikalisch korrekter `+3 A`. Ursache: der Action-Guard cappte nur bei `charging/full_charge/trickle` an `current_a`, ließ aber `idle` vollständig durch.

  Physikalische Ursache: `write_charge_current()` clampt Reg. 2705 immer auf mindestens `min_charge_current` (Zeile 584). Im PAUSE/idle-Modus steht Reg. 2705 also auf z.B. 3 A — DVCC begrenzt den Ladestrom entsprechend, unabhängig vom PV-Überschuss.

  **Neue universelle Formel (gilt für alle Stunden):**
  ```
  effective_setpoint_a = 0.0  falls current_a == 0.0  (Entladesperre)
                       = max(current_a, min_charge_current)  sonst
  planned_current_a = min(surplus_current_a, effective_setpoint_a)  falls surplus >= 0
                    = surplus_current_a                               falls surplus < 0
  ```

  Der Sonderfall `current_a == 0.0` (von `_simulate_hour()` bei SOC ≤ `floor_soc`, Victron ESS Entladesperre) wird explizit durchgereicht — `effective_setpoint_a` bleibt 0, kein ungewolltes Anheben auf `min_charge_current`.

  Ergebnisvergleich (Beispiel aus Livetest-Screenshot):

  | Stunde | Surplus | Action | v3.0.9.22 | v3.0.9.23 |
  |--------|---------|--------|-----------|-----------|
  | 07:00 | +0.16 kWh | PAUSE | +3.2 A ✗ | +3.0 A ✓ |
  | 08:00 | +0.68 kWh | PAUSE | +14.1 A ✗ | +3.0 A ✓ |
  | 09:00 | +1.35 kWh | PAUSE | +28.2 A ✗ | +3.0 A ✓ |
  | 11:00 | +1.79 kWh | LADEN | +20.0 A ✓ | +20.0 A ✓ |
  | 20:00 | −0.53 kWh | ENTLADEN | −11.0 A ✓ | −11.0 A ✓ |

---

## [3.0.9.22] – 2026-06-09

### Changed

- **Ladeplanung: `charge_current_a` zeigt tatsächlichen Stromfluss statt Reg-2705-Setpoint**

  Bisher zeigte die Spalte „Strom" im Ladeplan den konfigurierten Setpoint (Reg. 2705, z.B. 50 A bei Vollladung). Dieser Wert sagt nichts über den tatsächlichen Energiefluss aus — nachts steht Reg. 2705 auf 50 A, der Akku wird aber mit ca. −8 A entladen.

  Ab v3.0.9.22 wird der physikalisch zu erwartende bzw. tatsächlich geflossene Strom angezeigt:

  | Stunden | Berechnung |
  |---------|-----------|
  | Zukunft (Prognose) | `min(net_kwh × 1000 / V_nom, setpoint_a)` bei Ladung; direkt `net_kwh × 1000 / V_nom` bei Entladung |
  | Vergangenheit (Ist) | Integration von Reg. 842 (`battery_power_w`) über die Stunde → Wh / V_nom = mittlerer Strom [A] |

  Vorzeichen: **+ = Laden, − = Entladung** (konsistent mit Victron Reg. 841/842).

  Beispiel Nacht: `net_kwh = −0.4 kWh`, `V_nom = 48 V` → `−0.4 × 1000 / 48 = −8.3 A`. `min(−8.3, 50) = −8.3 A`.

### Added

- **`EnergyAccumulator.bat_wh`**: Neues Feld, integriert `state.battery_power_w` (Reg. 842) analog zu `pv_kwh`/`load_kwh`. Signed Wh, + = Laden, − = Entladen. `update()` erhält neuen Parameter `bat_w` (Default 0.0, rückwärtskompatibel).

- **`SystemState.bat_energy_today_wh`**: Brückenfeld zwischen `EnergyAccumulator` (main-Scope) und `_update_history()` (ChargeController-Scope).

- **`ChargeController._energy_base_bat`**: Neustart-Persistenz für `bat_wh`, analog zu `_energy_base_pv`/`_energy_base_load`. Wird in `_load_persistent` wiederhergestellt und in `_save_persistent` gespeichert. Verhindert, dass nach einem Neustart mid-day die Stundenbilanz nur den Post-Neustart-Anteil integriert. Tageswechsel-Reset auf 0.0 analog zu den anderen Basen.

- **`HourlyHistory.bat_energy_wh`**: Integrierter Batterieenergiefluss der Stunde [Wh], signed. Interne Felder `_hour_start_bat_wh` und `_raw_bat_wh` analog zu den bestehenden PV/Last-Kumulativen.

### Changed (Details)

- **`_update_history()`**: `charge_current_a` wird nicht mehr aus `charge_current_setpoint` gesetzt, sondern aus dem Wh-Integral berechnet:
  - Laufende Stunde: `bat_wh_hour / (V_nom × elapsed_h)` — laufend aktualisiert, `elapsed_h` minimum 1/60 h
  - Stundenabschluss: `bat_wh_hour / (V_nom × 1.0)` — Mittelwert über volle Stunde eingefroren
  - `bat_wh_total` = `state.bat_energy_today_wh + _energy_base_bat` (mit Neustart-Basis)
  - Debug-Log ergänzt: `BatStrom=+X.X A`

- **`build_schedule()`**: `planned_current_a` wird nur bei aktiven Lade-Actions (`charging`, `full_charge`, `trickle`) an den Setpoint (`current_a`) gecappt. Bei `idle`/`discharging` kommt der Wert ausschließlich aus der Energiebilanz (`net_kwh × 1000 / V_nom`). Verhindert, dass ein leicht positiver Überschuss bei `action=idle` fälschlich auf 0 A gecappt wird (`current_a = 0` in diesem Zustand).

- **Dashboard-Tabelle**: Spalte „Strom" zeigt Vorzeichen (`+`/`−`), grün für Ladung (`> +0.5 A`), rot für Entladung (`< −0.5 A`), grau für ~0 A. Prognosestunden auf `opacity: 0.6` gedimmt, Istwerte auf `1.0`.

---

## [3.0.9.21] – 2026-06-09

### Fixed

- **Simulation unterschritt ESS MinimumSocLimit (Reg 2901) im Notfall-Pfad und fehlerhafte Kommentare zur ESS-Entladesperre**

  `_simulate_hour()` verwendete im `emergency_charge_soc`-Block bei negativem PV-Überschuss `max(0.0, ...)` statt `max(floor_soc, ...)`. Da `emergency_charge_soc` (config) identisch mit Reg 2901 (20%) ist, sank der projizierte SOC im Ladeplan fälschlicherweise unter 20% — Dashboard zeigte z.B. 15.5%, 17.0% für frühe Nachtstunden.

  **Physikalisches Verhalten Victron ESS bei SOC ≤ Reg 2901:**
  - Die Batterie wird **nicht entladen** — Verbraucher werden aus dem Netz gespeist
  - Die Batterie wird **nicht aus dem Netz geladen** — der SOC bleibt konstant
  - State 11 (`SOC < MinSOC`): Entladesperre aktiv
  - State 12 (Minimal-Ladung aus Netz): erhöht den SOC nicht nennenswert, wird in der Simulation nicht modelliert
  - → **SOC friert bei `floor_soc` ein**, solange kein PV-Überschuss vorhanden ist

  Betroffene Stellen in `_simulate_hour()`, alle korrigiert:

  | Pfad | Vorher | Nachher |
  |------|--------|---------|
  | Notfall-SOC (kein PV) | `max(0.0, soc - deficit)` | Guard: nur Deficit abziehen wenn `soc > floor_soc`, sonst einfrieren |
  | `full_charge` (kein PV) | `max(floor_soc, soc - deficit)` immer | Guard analog: nur wenn `soc > floor_soc` |
  | `_apply_deficit()` | `max(floor_soc, ...)` ohne Guard | Logik korrekt, Kommentar präzisiert |
  | Morning-Branch (kein PV) | Kommentar falsch | Kommentar korrigiert |

  `floor_soc` wird von `build_schedule()` aus `state.evcc_min_soc` (= Reg 2901, aktuell 20%) abgeleitet und an `_simulate_hour()` übergeben.

  **Hinweis:** `emergency_charge_soc` in `config.yaml` ist rein informativer Fallback für den Fall dass Reg 2901 nicht ausgelesen werden kann. Der maßgebliche Wert ist immer Reg 2901.

- **`setup_logging()` Guard gibt falschen Rückgabetyp (Backlog-Fix)**

  `if logger.handlers: return logger` gab nur den Logger zurück statt des erwarteten Tuples `(logger, dedup_file, dedup_stream)`. Bei erneutem Aufruf (z.B. im Test oder nach Code-Reload) hätte `main()` mit `TypeError: cannot unpack non-iterable Logger object` gecrashed. In Produktion unkritisch (einzelner Aufruf), aber inkonsistente API.

  ```python
  # Vorher:
  if logger.handlers:
      return logger

  # Nachher:
  if logger.handlers:
      return logger, None, None
  ```

- **HTML-Versionsstring nicht aktualisiert**

  `<title>` im Dashboard-HTML zeigte noch `v3.0.9.20` statt `v3.0.9.21`.

---

## [3.0.9.20] – 2026-06-08

### Fixed

- **Trickle-Pfad ignorierte vorhandenen PV-Überschuss (Bug #2)**

  `decide()` Pfad 7 (Trickle) prüfte nur `soc < dyn_target - 10`, aber nicht ob tatsächlich Überschuss vorhanden war. Wenn `grid_w` kurzzeitig nahe 0 lag (z.B. unmittelbar nach evcc-Stop oder durch Messrauschen) ergab `surplus_w` fälschlicherweise 0 — obwohl PV − Last z.B. 1200 W Überschuss lieferte. Der Trickle-Pfad griff dann mit 3 A, obwohl Laden mit vollem Überschuss-Strom korrekt gewesen wäre.

  → Fix: Neue Variable `raw_surplus_w = max(0, pv_w - load_w)` als Guard:
  ```python
  # Vorher:
  if soc < dyn_target - 10:
      return gentle_a, "trickle", ...

  # Nachher:
  if soc < dyn_target - 10 and raw_surplus_w < 200:
      return gentle_a, "trickle", ...
  ```
  Trickle greift jetzt nur noch wenn wirklich kein PV-Überschuss vorhanden ist (z.B. abends bei PV 300 W, Last 800 W).

- **Surplus-Fallback zog Batterie-Ladestrom fälschlicherweise doppelt ab (Bug #1)**

  Im PV-Fallback-Pfad (wenn `abs(grid_w) ≤ 50`) wurde `surplus_from_pv = pv_w - load_w - battery_charge_w` berechnet. Da `load_w` AC-seitig gemessen wird und den Batterie-Ladestrom **nicht** enthält, war der Abzug von `battery_charge_w` eine Doppelkorrektur. Bei laufendem Laden (z.B. 21 A × 53 V ≈ 1100 W) wurde der verfügbare Überschuss um ~1100 W zu niedrig angesetzt → fälschlich kein Überschuss erkannt → Trickle statt Laden.

  → Fix: `surplus_from_pv` und `battery_charge_w` entfernt. Der Fallback verwendet jetzt direkt `raw_surplus_w`:
  ```python
  # Vorher:
  battery_charge_w = max(0.0, self.state.battery_power_w)
  surplus_from_pv  = max(0.0, pv_w - load_w - battery_charge_w)
  ...
  else:
      surplus_w      = surplus_from_pv
      surplus_source = "PV-Load-Batt"

  # Nachher:
  raw_surplus_w = max(0.0, pv_w - load_w)
  ...
  else:
      surplus_w      = raw_surplus_w
      surplus_source = "PV-Load"
  ```

- **Hysterese fror falsche Entscheidung bis zu 10 Minuten ein (Bug #3)**

  `force_new` in `run_cycle()` wurde nur bei SOC-Notfällen gesetzt. Massiver Export ins Netz (z.B. −1500 W nach evcc-Stop) und evcc-Statuswechsel lösten kein `force_new` aus — die veraltete Trickle- oder Idle-Entscheidung blieb bis zu `min_charge_duration_s` (~10 Minuten) aktiv, während der Wechselrichter Überschuss ins Netz einspeiste statt die Batterie zu laden.

  → Fix: Zwei neue `force_new`-Trigger:
  ```python
  # Massiver Export → sofort neu entscheiden
  if self.state.grid_power_w < -1000:
      force_new = True

  # evcc-Statuswechsel (Start/Stop) → sofort neu entscheiden
  evcc_now = self.state.evcc_active
  if evcc_now != getattr(self, "_last_evcc_active", evcc_now):
      force_new = True
  self._last_evcc_active = evcc_now
  ```
  `_last_evcc_active` wird mit dem aktuellen Wert initialisiert, um beim ersten Zyklus kein false positive auszulösen.

### Changed

- **Debug-Log `Surplus`-Zeile: `BattCharge=` → `raw=`**

  Der Debug-Log in `decide()` referenzierte die entfernte Variable `battery_charge_w`. Ersetzt durch `raw_surplus_w`:
  ```
  # Vorher:
  Surplus: Grid=0W, PV=2619W, Load=2526W, BattCharge=174W → surplus=0W (Quelle: PV-Load-Batt)

  # Nachher:
  Surplus: Grid=0W, PV=2619W, Load=2526W, raw=93W → surplus=0W (Quelle: PV-Load)
  ```

---

## [3.0.9.19] – 2026-06-07

### Fixed
- **Heartbeat-Thread erhielt NameError weil dedup_stream nicht in start_dashboard() sichtbar war**

  `dedup_stream` wurde in `main()` als lokale Variable erzeugt, war aber in `start_dashboard()` nicht sichtbar. Der Versuch, den Heartbeat-Thread in `start_dashboard()` zu starten, scheiterte mit `NameError: name 'dedup_stream' is not defined`.

  → Fix: `setup_logging()` gibt jetzt ein Tuple `(logger, dedup_file, dedup_stream)` zurück. `dedup_stream` wird als Parameter an `start_dashboard()` übergeben. `dedup_file` wird ebenfalls zurückgegeben für zukünftige Erweiterungen (sauberere API).

  ```python
  # Vorher:
  logger = setup_logging(cfg)
  # ...
  start_dashboard(cfg, state, logger)

  # Nachher:
  logger, dedup_file, dedup_stream = setup_logging(cfg)
  # ...
  start_dashboard(cfg, state, logger, dedup_stream)
  ```

- **Heartbeat-Timer wurde bei Bucket-Wechsel zurückgesetzt — Heartbeat-Zeitpunkt verschob sich**

  In v3.0.9.15 wurde bei einer neuen Nachricht (`msg != _last_msg`) der Timer `_last_ts = now` zurückgesetzt. Wenn zwischen zwei identischen [IDLE]-Einträgen z.B. ein einzelner Modbus-Fehler geloggt wurde, startete der 20-Minuten-Heartbeat-Timer von vorne. Ergebnis: Der [IDLE]-Heartbeat erschien deutlich später als erwartet (oder gar nicht, wenn ständig andere Nachrichten dazwischenkamen).

  → Fix: Bei Bucket-Wechsel wird `_last_ts` **nicht** mehr zurückgesetzt. Der 20-Minuten-Takt läuft absolut weiter, unabhängig davon ob zwischendurch andere Nachrichten erscheinen.

  ```python
  # Vorher:
  else:
      self._last_msg = msg
      self._last_ts = now      # ← Timer zurückgesetzt
      return True

  # Nachher:
  else:
      self._last_msg = msg
      # Timer NICHT zurücksetzen — absoluter Takt bleibt erhalten
      return True
  ```

- **FileHandler und StreamHandler teilten dieselbe DeduplicatingFilter-Instanz — Timer-Konflikt**

  In v3.0.9.15 wurde eine einzelne `DeduplicatingFilter`-Instanz auf **beide** Handler (FileHandler + StreamHandler/journald) angewendet. Wenn ein [IDLE]-Eintrag über den FileHandler lief, aktualisierte er `_last_ts` — und der StreamHandler sah den Timer als frisch zurückgesetzt. Der Journal-Heartbeat verschob sich dadurch systematisch, weil der FileHandler bei jedem Zyklus (alle 60s) einen Eintrag schrieb.

  → Fix: Je **eigene** `DeduplicatingFilter`-Instanz pro Handler:
  ```python
  dedup_file   = DeduplicatingFilter(...)   # nur für RotatingFileHandler
  dedup_stream = DeduplicatingFilter(...)   # nur für StreamHandler (journald)
  ```
  Beide Timer laufen völlig unabhängig. Der Journal-Heartbeat erscheint exakt alle 20 Minuten, auch wenn das Logfile ständig beschrieben wird.

- **Kein Heartbeat wenn Browser geschlossen und kein [IDLE]-Eintrag kam**

  Der `DeduplicatingFilter.filter()` wird nur aufgerufen wenn ein Log-Eintrag **eingeht**. Wenn der Browser-Tab geschlossen ist, kommen keine HTTP-Requests → Werkzeug loggt nichts. Wenn gleichzeitig der BatteryManager im stabilen Zustand ist (keine neuen Entscheidungen), kommt auch kein [IDLE]-Eintrag. Der Heartbeat-Timer läuft ab, aber niemand prüft ihn — es erscheint **gar kein** Heartbeat im Journal.

  → Fix: Neuer Hintergrund-Thread `_HeartbeatThread`, der alle 60 Sekunden `emit_heartbeat_if_due()` auf allen Dedup-Instanzen aufruft:
  ```python
  class _HeartbeatThread(threading.Thread):
      def run(self):
          while not self._stop.is_set():
              self._stop.wait(self._interval)
              for f in self._filters:
                  f.emit_heartbeat_if_due()
  ```
  Der Thread überwacht **beide** Dedup-Instanzen:
  - `dedup_stream` → [IDLE]-Heartbeats im Journal (BatteryManager-Logs)
  - `dedup_werkzeug` → HTTP-Heartbeats im Journal (Werkzeug-Access-Logs)

  Ergebnis: Auch bei geschlossenem Browser und stillem Betrieb erscheint alle 20 Minuten ein Heartbeat.

- **Thread-Safety: Race-Condition bei gleichzeitigem Log-Zugriff**

  `DeduplicatingFilter` wurde von mehreren Threads gleichzeitig aufgerufen (Hauptschleife + Flask-Worker-Threads für Dashboard-Requests). `_last_msg` und `_last_ts` wurden ohne Locking gelesen/geschrieben → theoretische Race-Condition bei gleichzeitigem Zugriff.

  → Fix: `threading.Lock()` um alle Zugriffe auf `_last_msg` und `_last_ts` in `filter()` und `emit_heartbeat_if_due()`.

### Added
- **`DeduplicatingFilter.emit_heartbeat_if_due()` — Hintergrund-Heartbeat ohne eingehenden Log-Eintrag**

  Neue Methode, die vom `_HeartbeatThread` aufgerufen wird. Prüft ob der Heartbeat fällig ist und schreibt einen **synthetischen** `LogRecord` direkt an `self._handler` — ohne erneut durch `filter()` zu laufen.

  ```python
  def emit_heartbeat_if_due(self) -> None:
      if not self._enabled or self._handler is None:
          return
      now = time.monotonic()
      with self._lock:
          if not self._last_msg:
              return
          if (now - self._last_ts) < self._heartbeat_s:
              return
          self._last_ts = now
          last_msg = self._last_msg
      # Direkt an Handler emitieren, Filter wird übersprungen
      if last_msg == "HTTP_ACCESS":
          text = "- (Heartbeat: kein Browser-Request seit 20min)"
      else:
          text = last_msg + " (Heartbeat)"
      r = logging.LogRecord(name="heartbeat", level=logging.INFO,
                            pathname="", lineno=0, msg=text, args=None, exc_info=None)
      self._handler.emit(r)
  ```

  Vorteil: `_last_msg` wird nicht durch einen Fremd-String korrumpiert. Der nächste echte Log-Eintrag wird weiterhin korrekt dedupliziert.

- **`DeduplicatingFilter._handler` — Handler-Referenz für synthetische Heartbeats**

  Nach der Handler-Erstellung in `setup_logging()` wird die Handler-Referenz in die Dedup-Instanz geschrieben:
  ```python
  dedup_stream._handler = ch   # StreamHandler
  dedup_file._handler = fh     # RotatingFileHandler
  ```
  Ermöglicht `emit_heartbeat_if_due()` das direkte Emitieren ohne Umweg über den Logger.

### Changed
- **`setup_logging()` Rückgabetyp: `tuple[Logger, DeduplicatingFilter, DeduplicatingFilter]`**

  Statt nur `Logger` wird jetzt ein Tuple zurückgegeben, damit `main()` die `dedup_stream`-Instanz an `start_dashboard()` übergeben kann.

- **`start_dashboard()` Signatur erweitert um `dedup_stream: DeduplicatingFilter`**

  Nimmt die BatteryManager-Stream-Dedup-Instanz entgegen und übergibt sie zusammen mit der neu erzeugten `dedup_werkzeug` an den `_HeartbeatThread`.

### Notes
- Die Zwischenversionen 3.0.9.16–3.0.9.18 wurden nie released. Alle Änderungen sind in dieser Version zusammengefasst.
- Der `_HeartbeatThread` läuft als `daemon=True` und beendet sich beim Programmende automatisch.
- Heartbeat-Intervall bleibt konfigurierbar via `config.yaml`: `logging.dedup_heartbeat_minutes` (Default: 20.0).

---

## [3.0.9.15] – 2026-06-06

### Fixed

- **Werkzeug Heartbeat-Zeilen erschienen als `"%s" %s %s (Heartbeat)` im Journal**

  Ursache: Werkzeug speichert Access-Log-Einträge intern als unformatiertes
  Template (`record.msg = '"%s" %s %s'`, `record.args = (method, url, status)`).
  Der alte Code hängte `" (Heartbeat)"` an `record.msg` *vor* der Expansion
  an und löschte danach `record.args`. Das Ergebnis war der literal
  unformatierte String im Journal statt des erwarteten Requests:
  ```
  # Vorher (falsch):
  Jun 06 14:00:15 ... "%s" %s %s (Heartbeat)

  # Jetzt (korrekt):
  Jun 06 14:00:15 ... "GET /api/state HTTP/1.1" 200 - (Heartbeat)
  ```
  Fix in `DeduplicatingFilter.filter()`: `formatted = record.getMessage()`
  wird *zuerst* aufgerufen (expandiert args), danach wird
  `record.msg = formatted + " (Heartbeat)"` gesetzt. So ist `record.msg`
  bereits der fertige String wenn der Handler ihn ausgibt.

- **`GET /` und `GET /api/state` lösten unabhängige Heartbeat-Timer aus**

  Da `_normalize()` bisher Methode + Pfad + Status als Schlüssel nutzte,
  wurden alle Dashboard-Routen (`/`, `/api/state`, …) als verschiedene
  Nachrichten behandelt und bekamen je einen eigenen 20-Minuten-Timer.
  Im Journal erschienen dadurch zu jedem Heartbeat-Zeitpunkt mehrere
  Einträge statt einem.

  Fix in `DeduplicatingFilter._normalize()`: Alle HTTP-Access-Log-Zeilen
  werden auf den einheitlichen Bucket `"HTTP_ACCESS"` normalisiert —
  unabhängig von Methode, Pfad und Statuscode. Zusätzlich wird Werkzeugs
  `%s`-Template-Format als zweites Muster abgefangen:
  ```python
  # Muster 1: vollständig formatierter Access-Log
  if re.match(r'^[\d\.]+\s+-\s+-\s+\[.+?\]\s+"(?:GET|...)...', msg):
      return "HTTP_ACCESS"
  # Muster 2: Werkzeug-internes %s-Format (Sicherheitsnetz)
  if re.match(r'^"%s"\s+%s\s+%s', msg):
      return "HTTP_ACCESS"
  ```
  Alle Dashboard-Requests teilen jetzt denselben Heartbeat-Bucket →
  genau ein Eintrag alle 20 Minuten im Journal.

### Changed

- **GUI-Footer Versionsstring entfernt**

  Die Zeile `v3.0.9.10 | Aktualisiert: HH:MM:SS – nächste in Xs` im
  Footer des Dashboards wurde entfernt. Der Header zeigt die Version
  bereits (`⚡ Solar Batterie Manager v3.0.9.15`), ein zweiter
  Versionsstring im Footer war redundant und zeigte zudem eine veraltete
  Versionsnummer (`v3.0.9.10`).

  Entfernt: CSS-Klasse `.ft`, HTML-Element `<div class="ft" id="ft">`,
  JS-Zeile `document.getElementById('ft').textContent = ...`.

---

## [3.0.9.14] – 2026-06-06

### Fixed
- **Werkzeug/Flask Access-Logs fluteten journalctl alle 30 Sekunden**
  Das Dashboard-JavaScript pollt `/api/state` alle 30 Sekunden. Der Werkzeug-
  Logger (`logging.getLogger('werkzeug')`) schrieb jede Request-Zeile
  ungefiltert ins Journal:
  ```
  192.168.168.60 - - [06/Jun/2026 11:26:03] "GET /api/state HTTP/1.1" 200 -
  ```
  Obwohl das Logfile (RotatingFileHandler) bereits vom `DeduplicatingFilter`
  gefiltert wurde, lief der Werkzeug-Logger über einen eigenen Handler
  (StreamHandler → journald) und ignorierte den Filter.

  → Fix: Zwei unabhängige Änderungen:

  **1. `DeduplicatingFilter._normalize()`**
  Neue Methode extrahiert den statischen Kern aus bekannten variablen
  Log-Mustern, bevor der Vergleich stattfindet:
  ```python
  def _normalize(self, msg: str) -> str:
      # Flask/Werkzeug Access-Log:
      # "192.168.168.60 - - [06/Jun/2026 11:26:03] \"GET /api/state HTTP/1.1\" 200 -"
      m = re.match(
          r'^[\d\.]+\s+-\s+-\s+\[.+?\]\s+"(GET|POST|...)\s+(\S+)\s+HTTP/\d\.\d"\s+(\d+)',
          msg)
      if m:
          method, path, status = m.groups()
          return f"{method} {path} HTTP/1.x {status}"
      return msg
  ```
  Variable Teile (IP, Datum, Uhrzeit) werden entfernt → alle 30-Sekunden-
  Requests auf `/api/state` werden als identisch erkannt.

  **2. `start_dashboard()`: Werkzeug-Logger mit dedupliziertem Handler**
  ```python
  werkzeug_log = logging.getLogger('werkzeug')
  werkzeug_log.handlers.clear()          # Default-Handler entfernen
  werkzeug_log.setLevel(logging.INFO)
  werkzeug_log.propagate = False         # Nicht zum Root-Logger durchreichen

  dedup_werkzeug = DeduplicatingFilter(...)
  ch_werkzeug = logging.StreamHandler()
  ch_werkzeug.addFilter(dedup_werkzeug)
  werkzeug_log.addHandler(ch_werkzeug)
  ```
  Der Werkzeug-Logger bekommt jetzt denselben `DeduplicatingFilter` mit
  Heartbeat wie die BatteryManager-Logs. Identische Requests werden
  unterdrückt, alle 20 Minuten (konfigurierbar) erscheint ein Heartbeat
  mit `(Heartbeat)`-Marker.

  **Ergebnis im Journal:**
  ```
  # Vorher (alle 30s):
  Jun 06 11:26:03 ... "GET /api/state HTTP/1.1" 200 -
  Jun 06 11:26:33 ... "GET /api/state HTTP/1.1" 200 -
  Jun 06 11:27:03 ... "GET /api/state HTTP/1.1" 200 -
  ...

  # Nachher (nur alle 20 Minuten):
  Jun 06 11:26:03 ... "GET /api/state HTTP/1.1" 200 - (Heartbeat)
  Jun 06 11:46:03 ... "GET /api/state HTTP/1.1" 200 - (Heartbeat)
  ```
  Das Logfile `battery_manager.log` war bereits clean (kein Werkzeug-Output),
  daher betraf das Problem nur journalctl / stdout.

### Notes
- `_normalize()` ist erweiterbar: weitere variable Log-Muster können bei
  Bedarf hinzugefügt werden (z.B. Modbus-Retry-Timestamps).
- Keine neue Config-Option nötig — `dedup_enabled` und `dedup_heartbeat_minutes`
  aus `config.yaml` greifen auch für Werkzeug-Logs.

---

## [3.0.9.13] – 2026-06-06

### Fixed
- **DeprecationWarning: `datetime.utcfromtimestamp()` ist deprecated**
  Python 3.12+ warnt: `datetime.datetime.utcfromtimestamp() is deprecated and scheduled for removal in a future version. Use timezone-aware objects to represent datetimes in UTC`.

  Aufgetreten in `VrmForecastManager.fetch()` bei der Log-Ausgabe der UTC-Zeitbereiche:
  ```python
  # Vorher (deprecated):
  datetime.utcfromtimestamp(start_unix)

  # Nachher (timezone-aware):
  datetime.fromtimestamp(start_unix, tz=timezone.utc)
  ```

  Zusätzlich: `timezone` zum `datetime`-Import hinzugefügt:
  ```python
  from datetime import datetime, timedelta, date, timezone
  ```

  Keine funktionale Änderung — die Log-Ausgabe bleibt identisch, nur ohne Deprecation-Warnung.

### Added
- **Deduplizierungs-Filter mit Heartbeat — eingebautes `reduce_log_file.sh`**
  Die Idee aus `reduce_log_file.sh` (aufeinanderfolgende identische Zeilen filtern) ist jetzt direkt im Python-Logging eingebaut. Statt externer Post-Processing entsteht das reduzierte Log bereits beim Schreiben.

  **Problem:** Bei stabilem Zustand (z.B. 10 Minuten "[IDLE] 3A | Nacht: kein Laden (SOC 55.0%)") produziert das Log 10 identische Zeilen pro Stunde. Bei 24h Betrieb entstehen so leicht 500+ Zeilen, die identisch sind.

  **Lösung:** Neuer `DeduplicatingFilter` (logging.Filter-Subclass):
  - Vergleicht nur den Nachrichtentext (ohne Zeitstempel/Level, wie `reduce_log_file.sh`)
  - Identische Nachrichten werden unterdrückt
  - **Heartbeat:** Alle 20 Minuten (konfigurierbar) wird trotzdem eine Zeile ausgegeben, damit man sieht dass das Programm noch lebt — mit "(Heartbeat)"-Suffix

  **Konfiguration** (optional in `config.yaml`):
  ```yaml
  logging:
    dedup_enabled: true              # Default: true
    dedup_heartbeat_minutes: 20.0    # Default: 20 Minuten
  ```

  **Beispiel-Log-Ausgabe:**
  ```
  2026-06-06 10:00:15 [INFO] [IDLE] 3A | Nacht: kein Laden (SOC 55.0%)
  2026-06-06 10:20:15 [INFO] [IDLE] 3A | Nacht: kein Laden (SOC 55.0%) (Heartbeat)
  2026-06-06 10:21:15 [INFO] [IDLE] 3A | Nacht: kein Laden (SOC 54.0%)
  2026-06-06 10:41:15 [INFO] [IDLE] 3A | Nacht: kein Laden (SOC 54.0%) (Heartbeat)
  ```

  **Vorteile gegenüber externem `reduce_log_file.sh`:**
  - Kein zusätzlicher Cronjob oder manueller Aufruf nötig
  - Heartbeat zeigt Lebendigkeit auch bei langem IDLE-Zustand
  - FileHandler und StreamHandler (journalctl) werden gleichermaßen gefiltert
  - Rückwärtskompatibel: fehlende Config-Einträge → Defaults greifen

  **Implementierung:**
  - `DeduplicatingFilter`: Filter-Unterklasse, speichert `_last_msg` und `_last_ts`, prüft `time.monotonic()` gegen Heartbeat-Intervall
  - Wird auf **jeden Handler** (File + Stream) angewendet, damit sowohl Logfile als auch journalctl/journalctl dedupliziert werden
  - Bei Programmstart wird eine Info-Zeile ausgegeben: `Deduplizierung aktiv: identische Zeilen werden unterdrückt, Heartbeat alle 20 Minuten`
  - Heartbeat-Marker wird direkt ins `record.msg` geschrieben (`+ " (Heartbeat)"`), kein separater Formatter nötig — `record.args = None` verhindert %-formatting-Probleme mit den Klammern im Marker

### Notes
- `reduce_log_file.sh` kann weiterhin verwendet werden (z.B. für alte Logs), ist aber für den laufenden Betrieb nicht mehr nötig.
- Die Deduplizierung betrifft nur identische Nachrichten. Unterschiedliche SOC-Werte, Stromwerte oder Modi werden wie gewohnt geloggt.
- Heartbeat-Intervall sollte länger sein als `control_interval_seconds` (Default 60s), sonst hat der Filter praktisch keine Wirkung. 20 Minuten ist ein guter Kompromiss zwischen Log-Größe und Beobachtbarkeit.

### Fixed
- **Unnötiges Hoch-/Runterrampen bei PV-Überschuss — Ladestrom nicht an tatsächlichem Überschuss orientiert**
  Bei geringem PV-Überschuss (z.B. 292 W) wurde der Ladestrom trotzdem auf `max_a` (50 A) hochgerampet, weil `surplus_w > 200` direkt `max_a` zurückgab. Der Ramp-Mechanismus führte das dann in 5-Schritten hoch (10→20→30→40→50 A), obwohl 292 W / 50 V = 5,8 A → maximal ~6 A sinnvoll gewesen wären. Anschließend fiel die Entscheidung zurück auf IDLE (kein Überschuss mehr) und der Strom wurde in gleichen Schritten wieder heruntergerampet — völlig unnötige Modbus-Schreibvorgänge und Netzbezug.

  Ursache: Die Überschuss-Berechnung `surplus_w = max(0.0, pv_w - load_w)` berücksichtigte den **aktuellen Batterieladestrom nicht**. Wenn der Akku bereits mit z.B. 10 A lädt, fließen diese ~500 W in `pv_w` mit rein, werden aber nicht abgezogen — der vermeintliche Überschuss bleibt hoch, obwohl gar keiner mehr da ist. Zudem wurde der Ladestrom nie an den tatsächlich verfügbaren Überschuss gekoppelt.

  → Fix: Zwei unabhängige Änderungen:

  **1. Grid-basierte Überschuss-Ermittlung (zuverlässiger)**
  ```python
  grid_w = self.state.grid_power_w
  # Export = negativer Grid-Wert = wirklicher Überschuss
  surplus_from_grid = max(0.0, -grid_w) if grid_w < -50 else 0.0
  ```
  Grid-Power ist die zuverlässigste Quelle für "wirklicher Überschuss", weil sie alle Verluste, DC-Lasten und den aktuellen Batterieladestrom automatisch mitberücksichtigt. Negativer Grid-Wert = Export = verfügbarer Überschuss. Positiver Grid-Wert = Import = kein Überschuss.

  **2. Ladestrom auf tatsächlichen Überschuss limitiert**
  ```python
  max_from_surplus = surplus_w / actual_v if surplus_w > 0 else 0.0
  charge_a = min(max_from_surplus, max_a)
  # Beispiel: 292W / 50V = 5,8A → max 6A, nicht 50A!
  ```
  Der Ladestrom wird jetzt nie höher gesetzt als der verfügbare Überschuss erlaubt. Überschuss < 200 W → kein Laden. Überschuss 500 W → ~10 A. Überschuss 2500 W → 50 A (oder was `max_a` erlaubt).

  **3. PV-basierter Fallback (wenn Grid-Messung unzuverlässig)**
  ```python
  battery_charge_w = max(0.0, self.state.battery_power_w)
  surplus_from_pv = max(0.0, pv_w - load_w - battery_charge_w)
  ```
  Wenn die Grid-Messung nahe 0 oder unzuverlässig ist, wird als Fallback `PV - Load - Battery_Charge` verwendet. Der aktuelle Batterieladestrom wird explizit abgezogen, damit derselbe Strom nicht doppelt gezählt wird.

  **4. Korrekte Log-Ausgabe**
  Statt irreführender Meldungen wie `PV-Ueberschuss 292 W -> 50 A` jetzt transparent:
  ```
  PV-Überschuss 292W [Grid] → max 5.8A, setze 6A (SOC 68.0% → Ziel 73%)
  ```
  Die Quelle des Überschusswerts (`[Grid]` oder `[PV-Load-Batt]`) wird mitgeloggt.

  **5. Auch im Optimal-Fenster durch Überschuss limitiert**
  Bisher wurde im Optimal-Fenster der dynamisch berechnete Strom (`required_a`) gesetzt, ohne zu prüfen ob überhaupt so viel Überschuss vorhanden ist. Jetzt:
  ```python
  charge_a = min(max_from_surplus, required_a, reduced_a)
  ```
  Der Strom ist jetzt in allen Pfaden durch den verfügbaren Überschuss begrenzt — nie mehr Strom aus dem Netz ziehen als nötig.

### Changed
- **Surplus-Berechnung: Grid-Bezug als primäre Quelle**
  Die Reihenfolge der Überschuss-Ermittlung wurde umgedreht: Grid-basiert (zuverlässig) hat Priorität, PV-basiert (Fallback) nur wenn Grid-Messung unzuverlässig. Die Grid-Messung reflektiert die physikalische Realität am Hausanschluss — alles was exportiert wird, könnte stattdessen in den Akku fließen.

### Notes
- Grid-basierte Überschuss-Ermittlung funktioniert nur wenn ein bidirektionaler Zähler vorhanden ist (typisch bei Victron ESS-Installationen). Bei fehlendem/inkorrektem Grid-Meter fällt der Code automatisch auf PV-Load-Battery zurück.
- Die Änderung ist rückwärtskompatibel — keine neuen Config-Optionen nötig.

### Fixed
- **Asymmetrische Hysterese beim Ziel-SOC — systematischer 2%-Unterschuss**
  Der "Ziel erreicht"-Block verwendete `soc >= dyn_target - hyst` als Abschalt-
  bedingung. Bei `dyn_target = 66%` und `hyst = 2%` wurde bereits bei `SOC = 64%`
  als "Ziel erreicht" gewertet und der Ladestrom auf 0 gesetzt. Da der SOC danach
  weiter in die Nacht entlud, startete die nächste Nacht systematisch 2% unter dem
  berechneten Ziel.

  Ursache: Die Hysterese wirkte symmetrisch — sowohl als Einschaltschwelle
  (`soc < dyn_target - hyst` → wieder laden) als auch als Abschaltschwelle
  (`soc >= dyn_target - hyst` → Ziel "erreicht"). Das führte dazu, dass der
  Sollwert nie wirklich erreicht wurde, nur die untere Hysterese-Grenze.

  → Fix: Abschaltschwelle auf `soc >= dyn_target` angehoben (kein Unterschuss
  mehr). Einschaltschwelle bleibt bei `soc < dyn_target - hyst` — die Hysterese
  wirkt jetzt asymmetrisch, nur als Schutz gegen erneutes Einschalten, nicht
  als Abschaltschwelle.

  ```python
  # Vorher (falsch — stoppt bei dyn_target - hyst, z.B. 64% statt 66%):
  if soc >= dyn_target - hyst:
      return 0, "idle", ...

  # Nachher (korrekt — stoppt erst wenn Ziel wirklich erreicht):
  if soc >= dyn_target:
      return 0, "idle", ...
  ```

  Effekt: Laden bis exakt `dyn_target`, Nachladen erst unter `dyn_target - hyst`.
  2% mehr Energie in der Nacht, kein Oszillationsrisiko.

- **Irreführender Logtext bei Morgen-Verzögerung mit ausreichender PV-Prognose**
  Wenn `pv_in_optimal >= needed_kwh` aber `soc < min_required` (SOC zu niedrig
  zum Warten), lautete die Log-Meldung:

  ```
  Morgen: PV nicht ausreichend im Optimal-Fenster (20.0 kWh < 5.2 kWh), fruehes Laden noetig
  ```

  Der Vergleich im Text war faktisch falsch (20.0 > 5.2) — der eigentliche
  Ablehnungsgrund war der niedrige SOC, nicht die PV-Prognose.

  → Fix: Logtext unterscheidet jetzt zwischen zwei Fällen:

  ```
  # Fall a: PV ausreichend, aber SOC zu niedrig zum Warten:
  Morgen: SOC 33.0% < min 35%, kann nicht warten; PV im Fenster ausreichend (20.0 kWh >= 5.2 kWh), fruehes Laden noetig

  # Fall b: PV wirklich nicht ausreichend:
  Morgen: PV nicht ausreichend im Optimal-Fenster (3.1 kWh < 5.2 kWh), fruehes Laden noetig
  ```

- **`_simulate_hour()`: Ziel-SOC-Abbruch inkonsistent mit `decide()` (Nachreview)**
  Nach dem Fix in `decide()` stand in `_simulate_hour()` noch die alte Bedingung
  `if soc_sim >= dyn_target - hyst`. Die Simulation stoppte das Laden 2% früher
  als die Realsteuerung — der Ladeplan zeigte "idle" obwohl der echte Regler noch
  lud. `_simulate_hour()` verwendet jetzt ebenfalls `soc_sim >= dyn_target`.

- **`_simulate_hour()` / `decide()`: `hours_left` mit Minutengenauigkeit**
  `hours_left = max((opt_end - h_now) + 1, 0.5)` arbeitete mit ganzen Stunden.
  Um 11:59 im Fenster 11–15 lieferte das 1.0h statt ~3.0h — der berechnete
  Ladestrom war dreifach überhöht. Korrigiert auf:
  ```python
  hours_left = max((opt_end + 1.0) - h_now - minute_now / 60.0, 0.5)
  ```

- **`decide()`: Spannungs-Fallback mit hartem Minimum bei Modbus-Ausfall**
  `actual_v = max(battery_voltage, nom_v * 0.9)` konnte bei Modbus-Ausfall
  `battery_voltage = 0.0` liefern, Fallback dann `48 * 0.9 = 43.2V`. Für eine
  14-kWh-LFP-Anlage unrealistisch niedrig, führte zu überhöhten `required_a`.
  Neuer Boden: `max(battery_voltage, nom_v * 0.875, 42.0)` — 42V ist die absolute
  LFP-Untergrenze (0% SOC), darunter ist kein realistischer Betrieb möglich.

- **Dashboard-Versionsnummer nicht vollständig aktualisiert (Nachreview)**
  `<h1>`-Tag und Footer-String zeigten noch `v3.0.9.9`. Alle vier Stellen
  (HTML-Title, h1-Span, JS-Footer-String, `logger.info` in `main()`) auf
  `v3.0.9.10` aktualisiert.

---

### Fixed
- **GUI zeigt 0 A obwohl Cerbo auf 3 A steht — Anzeige-Bug bei Clamping**
  Wenn `decide()` 0 A zurückgibt (idle), aber der tatsächlich geschriebene Wert
  durch `set_max_charge_current()` auf `min_charge_current` (z.B. 3 A) geclamped
  wird, zeigte das Dashboard fälschlicherweise 0 A statt 3 A.

  Ursache: `_last_written_ramped_a` speicherte den ungeclamppten Rampenwert
  (z.B. 5 A), nicht den tatsächlich geschriebenen Wert (3 A). Bei der nächsten
  Hysterese-Prüfung (`abs(ramped - last_written) >= 1.0`) driftete der Vergleich,
  weil `last_written` 5 enthielt, aber die Hardware 3 stand.

  → Fix: Nach erfolgreichem Write wird `_last_written_ramped_a` jetzt auf
  `state.charge_current_setpoint` gesetzt (der Wert, den `set_max_charge_current()`
  tatsächlich geschrieben hat, nach Clamping). Die Hysterese vergleicht jetzt
  gegen den echten Hardware-Wert, nicht gegen den theoretischen Rampenwert.

  ```python
  # Vorher (falsch):
  self._last_written_ramped_a = ramped  # z.B. 5 A (ungeclamppt)

  # Nachher (korrekt):
  self._last_written_ramped_a = self.state.charge_current_setpoint  # z.B. 3 A (tatsächlich geschrieben)
  ```

---

## [3.0.9.8] – 2026-06-01

### Fixed
- **Trickle-Block (Step 7) verwendete fälschlich `trickle_current` (20 A) statt sanftem Strom**
  In v3.0.9.7 wurde `trickle_current` von 5 A auf 20 A erhöht, damit das Cellbalancing bei
  SOC ≥ 98% funktioniert (BMS braucht Strom für Balancing). Der Trickle-Block (Step 7)
  verwendete jedoch denselben Wert — bei SOC weit unter Ziel (z.B. 55% bei Ziel 80%)
  wurde mit 20 A aus dem Netz geladen, obwohl kein PV-Überschuss vorhanden war.

  → Fix: Trickle-Block verwendet jetzt `min_charge_current` (z.B. 3 A) statt `trickle_a`:
  ```python
  gentle_a = self.bat.get("min_charge_current", 5)
  return gentle_a, "trickle", ...
  ```

  - Cellbalancing-Block (SOC ≥ 98%): Weiterhin `trickle_a` = 20 A (BMS-Anforderung)
  - Trickle-Block (SOC weit unter Ziel, kein PV): Jetzt `gentle_a` = `min_charge_current` (Default 3 A)
  - Keine neue Config-Option nötig — verwendet bestehendes `battery.min_charge_current`

---

# Changelog — battery_manager.py

## [3.0.9.7] – 2026-06-01

### Changed
- **Schritt 6, Optimal-Fenster: Dynamischer Strom statt fester `reduced_a`**
  Bisher wurde im Optimal-Fenster immer pauschal `reduced_charge_current_a` (Default 20 A) geladen.
  Jetzt wird der Strom aus dem verbleibenden Energiebedarf berechnet:

  ```
  missing_kwh = (dyn_target - soc) / 100 * capacity_kwh
  hours_left  = max((opt_end - h_now) + 1, 0.5)
  required_kw = (missing_kwh * 1.15) / hours_left   # +15% Reserve
  required_a  = (required_kw * 1000) / actual_v
  charge_a    = max(optimal_min_a, min(required_a, reduced_a))
  ```

  - `actual_v`: Echte Batteriespannung aus Modbus (mit Fallback auf `nom_v * 0.9`)
  - `optimal_min_a`: Neue Config-Option `charging.optimal_window_min_current_a` (Default 10 A)
  - `reduced_a`: Bleibt als harte Obergrenze erhalten (Default 20 A)
  - Guard: Wenn `missing_kwh <= 0.1` → sanftes Laden mit `min_charge_current` (z.B. 3 A)

### Added
- **Config-Option `charging.optimal_window_min_current_a`** (optional, Default 10 A)
  Mindeststrom im Optimal-Fenster. Verhindert, dass der dynamische Algorithmus bei
  geringem Energiebedarf unrealistisch niedrige Ströme (z.B. 2–3 A) vorschlägt,
  die DVCC ignorieren würde.

### Removed
- **Unbenutzte Variable `nom_v` aus `decide()`** entfernt.
  Die Nennspannung wird jetzt nur noch in `_simulate_hour()` verwendet.

### Notes
- Die 15% Sicherheitsreserve (`* 1.15`) puffert typische Forecast-Abweichungen
  und Verluste (Wirkungsgrad, Temperatur) ab.
- Der Guard `missing_kwh <= 0.1` verhindert Überladung bei fast erreichtem Ziel.
  Er verwendet `min_charge_current` (z.B. 3 A), nicht `trickle_a` (20 A),
  um Verwechslung mit dem Cellbalancing-Block zu vermeiden.
- `trickle_a` (20 A) bleibt exklusiv für den Cellbalancing-Block bei SOC >= 98%.


============================================================

# Changelog — battery_manager.py

## [3.0.9.6] – 2026-06-01

### Changed
- **Proportionalladung vollständig entfernt — feste Stromwerte in allen Pfaden**  
  Die Berechnung `surplus_a = surplus_w / nom_v` (Ladestrom proportional zum PV-Überschuss) wurde ersatzlos aus allen drei Stellen in `decide()` entfernt:

  1. **Morgen-Notladung** (`soc < min_required`): Bisher wurde bei `surplus_w > 200 W` proportional geladen (`min(surplus_a, max_a)`), bei fehlendem Überschuss optional `trickle_a` oder 0 A. Das erzeugte unnötige Modbus-Schreibvorgänge und verlangsamte die SOC-Erholung in einer Notlage.  
     → Jetzt: Immer `max_a`, kein PV-Überschuss-Check, kein `morning_trickle_on_no_pv`-Flag mehr.

  2. **Schritt 6, Optimal-Fenster** (`opt_start <= h_now <= opt_end`, `pv_in_optimal >= needed_kwh * 1.5`): Bisher `min(surplus_a, reduced_a)`.  
     → Jetzt: Direkt `reduced_a` (aus `charging.reduced_charge_current_a`, Default 20 A).

  3. **Schritt 6, Normalbetrieb** (`surplus_w > 200 W`): Bisher `min(surplus_a, max_a)`.  
     → Jetzt: Direkt `max_a`.

### Removed
- **Config-Option `charging.morning_trickle_on_no_pv`** entfällt — der zugehörige Pfad existiert nicht mehr. Bestehende Einträge in `config.yaml` werden ignoriert und können entfernt werden.

---

## [3.0.9.5] – 2026-06-01

### Fixed
- **`_simulate_hour()`: Notfall-SOC block ignorierte `floor_soc` (Reg 2901)**  
  Der Ladeplan zeigte Entladung bis 15.3% (04:00), 16.9% (03:00), 18.4% (02:00) — obwohl das `ESS MinimumSocLimit` (Reg 2901) auf z.B. 20% steht. Die reale Hardware würde bei 20% stoppen (State 11), die Simulation sank aber tiefer.  
  Ursache: Im Notfall-SOC-Block (`soc_sim <= emergency_charge_soc`) wurde `max(0.0, ...)` verwendet statt `max(floor_soc, ...)`. Der `floor_soc`-Parameter (der Reg 2901 enthält) wurde ignoriert.  
  → Fix: `soc_sim = max(floor_soc, soc_sim - (deficit / cap) * 100)` — die Simulation sinkt jetzt korrekt nur bis zur harten Victron-Untergrenze (Reg 2901), nie darunter.

### Verified
- `_apply_deficit()`: ✅ verwendet `max(floor_soc, new_soc)`  
- `needs_full`-Block: ✅ verwendet `max(floor_soc, soc_sim - ...)`  
- Morgen-Notladung (discharging): ✅ verwendet `max(floor_soc, soc_sim - ...)`  
- Notfall-SOC: ✅ jetzt auch `max(floor_soc, soc_sim - ...)` (v3.0.9.5 Fix)

---

## [3.0.9.4] – 2026-05-31

### Fixed
- **`forecast_pv_remaining_kwh` wurde nie berechnet**  
  Das Feld `forecast_pv_remaining_kwh` in `SystemState` wurde nur deklariert, aber **nie** aktualisiert. Das Dashboard zeigte daher permanent "Verbleibend: 0.0 kWh", obwohl die VRM-Prognose deutlich höhere Restwerte lieferte.  
  → Fix: `forecast_pv_remaining_kwh` wird jetzt bei jedem Prognose-Update berechnet als Summe der PV-Prognose ab aktueller Stunde (`sum(f.pv_kwh for f in fc if f.hour >= now_h)`). Auch beim Programmstart wird der Wert korrekt vorbelegt.

- **VRM-Prognose wurde bei Wetteränderung nicht aktualisiert**  
  Die PV-Prognose im Dashboard zeigte z.B. **53.2 kWh**, während die VRM-Realität (rechts im Screenshot) bereits auf **32–39 kWh** korrigiert hatte. Ursache: `ForecastManager.get_forecast()` lieferte seinen eigenen lokalen Cache, auch wenn VRM neue Daten hatte. Der `force=True`-Parameter wurde nur beim Tageswechsel oder manuellem Aufruf übergeben, nicht bei regulären Updates.  
  → Fix: Wenn VRM aktiviert ist (`forecast.vrm.enabled == True`), wird bei jedem Prognose-Update-Intervall `force=True` an `get_forecast()` übergeben. VRM's Server cached selbst und gibt bei identischer Anfrage schnell eine 304-ähnliche Antwort zurück — kein Performance-Problem. Die lokale Cache-Invalidierung stellt sicher, dass der `ForecastManager`-Cache ebenfalls aktualisiert wird, wenn VRM neue Daten liefert.

- **VRM-Cache ohne Debug-Transparenz**  
  Es war nicht ersichtlich, ob VRM-Daten aus dem lokalen Cache oder vom Server kamen.  
  → Fix: `VrmForecastManager.fetch()` loggt jetzt im DEBUG-Level "VRM: liefere gecachte Prognose" wenn der lokale Cache verwendet wird, und "VRM-Prognose aktualisiert: X.X kWh heute" wenn der Server neu abgefragt wurde.

### Changed
- **Prognose-Update-Intervall: VRM bevorzugt**  
  Wenn VRM als Prognosequelle aktiv ist, wird bei jedem konfigurierten `update_interval_minutes`-Zyklus der VRM-Server direkt abgefragt (`force=True`). Der lokale Cache im `ForecastManager` wird entsprechend invalidiert. Open-Meteo/Solcast-Fallback bleibt unverändert (Cache nach Intervall).

---

## [3.0.9.3] – 2026-05-31

### Fixed
- **`decide()`: `PowerSmoother.update()` dezentral aufgerufen — Refactoring**  
  In v3.0.9.2 wurde `_power_smoother.update()` an zwei Stellen in `decide()` aufgerufen: Morgen-Notladung und PV-Überschuss (Schritt 6). Beide Pfade teilten sich denselben Smoother-State, ohne dass das strukturell erzwungen wurde. Bei zukünftigen Erweiterungen bestand die Gefahr eines echten Doppelaufrufs.  
  → Fix: `update()` wird jetzt **einmal zentral** am Anfang von `decide()` aufgerufen, vor allen Verzweigungen. Alle nachfolgenden Blöcke (Morgen-Notladung, PV-Überschuss, Trickle, Warten) nutzen dieselben geglätteten Werte (`pv_w`, `load_w`, `surplus_w`). Keine Fragilität mehr.

- **`ChargeController.__init__()`: `_power_smoother` außerhalb des Hauptblocks**  
  In v3.0.9.2 wurde `_power_smoother` nach dem Haupt-Init-Block (nach `_balancing_reset_date`) instanziiert. Stilistisch unordentlich, Wartbarkeit leidet.  
  → Fix: `_power_smoother` ist jetzt im Haupt-Init-Block, vor `_balancing_reset_date`.

### Changed
- **`decide()`: Morgen-Notladung ohne Überschuss — konfigurierbar**  
  In v3.0.9.2 wurde bei fehlendem PV-Überschuss im Morgenfenster immer `trickle_a` zurückgegeben (statt 0 A). Das verhindert zwar Oszillation, lädt aber auch um 6:30 Uhr ohne jede PV-Erzeugung mit 5 A (~240 W) aus dem Netz — eine Verhaltensänderung gegenüber dem ursprünglichen Design (0 A = warte auf PV).  
  → Fix: Neuer optionaler Config-Parameter `charging.morning_trickle_on_no_pv` (Default: `true`). Bei `true` = trickle bei fehlendem Überschuss (v3.0.9.2-Verhalten). Bei `false` = 0 A, warte auf PV (ursprüngliches Verhalten). Rückwärtskompatibel: fehlender Eintrag = `true`.

### Added
- **Config-Option `charging.morning_trickle_on_no_pv`** (optional, Default `true`): Steuert ob bei Morgen-Notladung ohne PV-Überschuss trickle geladen oder gewartet wird.

---

## [3.0.9.2] – 2026-05-31

### Fixed
- **`run_cycle()`: `force_new` deaktiviert Hysterese bei knapp unter `min_soc`**  
  Wenn der SOC knapp unter `min_soc` lag (z.B. 19 % bei `min_soc = 20 %`), wurde bei **jedem Zyklus** (jede Minute) eine komplett neue Entscheidung erzwungen (`force_new = True`). Die konfigurierte Hysterese (`min_charge_duration_minutes`, default 10 Minuten) war damit wirkungslos. `decide()` lieferte bei jedem Zyklus einen neuen Zielstrom basierend auf ungeglätteten Momentanwerten (PV-Leistung, Last, ESS-State). Die `_ramp()` konnte diesem Ziel nicht folgen und erzeugte das wellenartige Hoch-/Runterfahren (z.B. 10→20→30→20→30→40→30→20→10→0 A).  
  → Fix: `force_new` wird erst bei deutlicher Unterschreitung (`min_soc - 2 %`) sofort ausgelöst. Knapp darunter nur noch alle **2 Minuten** neu entschieden (nicht jede Minute). Die 10-Minuten-Hysterese kann endlich wirken.

- **`decide()`: Morgen-Notladung als harter 0/50-A-Schalter**  
  Die Bedingung `if pv_w > load_w + 200: return max_a else: return 0` war ein harter Ein/Aus-Schalter. Wenn eine Wolke vorbeizog oder der Kühlschrank ansprang (`load_w` stieg kurz), fiel die Entscheidung innerhalb einer Minute von 50 A auf 0 A. Dann rampte `_ramp()` 5 A pro Zyklus runter. Wolke weg → wieder hoch. Das erklärte exakt das Log-Muster (10→20→30→20→30→40→30→20→10→0…).  
  → Fix: Statt `max_a` wird der **proportionale Überschussstrom** geladen (`surplus_a = surplus_w / nom_v`), nie unter `trickle_a` (z.B. 5 A). Kurze Wolken oder Lastspitzen führen nicht mehr zum sofortigen Abschalten. Der Sollwert bleibt in einem stabilen Band statt zwischen 0 und 50 A zu springen.

- **`decide()`: PV-Überschuss basiert auf ungeglätteten Momentanwerten**  
  Der Ladestrom wurde direkt aus dem **aktuellen** PV-Überschuss berechnet (`pv_power_w - load_power_w`). Bei wechselhaftem Wetter oszillierte der Sollwert ständig. Kombiniert mit `force_new` (weil SOC < min_soc) entstand das Ping-Pong.  
  → Fix: Neue Klasse `PowerSmoother` mit gleitendem Durchschnitt über **3 Zyklen** (konfigurierbar via `power_smooth_window_cycles` in `config.yaml`, Default 3). PV- und Last-Leistung werden geglättet, bevor der Überschuss berechnet wird. Ein kurzer Wolkenbruch (1 Zyklus = 1 Minute) wird auf 1/3 des Wertes gedämpft. Der Ladestrom sinkt sanft statt abrupt. Bei dauerhaftem Wetterwechsel reagiert er trotzdem innerhalb von 3 Zyklen voll. Der Glättungspuffer wird bei **Tageswechsel automatisch geleert**.

### Added
- **`PowerSmoother`**: Gleitender Durchschnitt für PV- und Last-Leistung. Geglättete Werte für stabileren Ladestrom ohne Reaktionsverlust.
- **Config-Option `charging.power_smooth_window_cycles`** (optional, Default 3): Anzahl der Zyklen für die Glättung. 1 = keine Glättung, 3 = empfohlen, 5 = sehr träge.

### Notes
- Fix C (ESS-State-Hysterese) wurde bewusst **nicht** umgesetzt. Victron hat interne Hysterese für State 10↔11 (mindestens +3 % SOC), ein zusätzlicher Software-Timer ist überflüssig.
- Die drei Fixes (A, B, D) greifen zusammen: **A** gibt der Hysterese Zeit zu wirken, **B** verhindert das harte Ein/Aus-Schalten, **D** dämpft kurzzeitige Messwertschwankungen.

---

## [3.0.9] – 2026-05-30

### Added
- **Morgen-Verzögerung dynamisch (Sonnenaufgang)**  
  `morning_delay_start_hour` / `morning_delay_end_hour` entfallen. Stattdessen neuer Parameter `morning_delay_h` (Default: 4h). Das Morgenfenster beginnt jetzt automatisch beim Sonnenaufgang (GPS+Datum) und dauert `morning_delay_h` Stunden. `effective_morn_e = max(morn_e, opt_start)` bleibt erhalten, damit keine Lücke zum Optimal-Fenster entsteht.

- **`_get_optimal_charge_window()`: Dynamischer Sonnenhöchststand**  
  Bisher hardcoded `noon = 13`. Jetzt wird der Sonnenhöchststand aus `_calculate_sun_times()` übernommen (lokale Zeit, gerundet). Sommer-/Winterzeit und Längengrad werden korrekt berücksichtigt. `solar_noon_offset_hours` bleibt konfigurierbar.

- **`ForecastManager._calculate_sun_times()`**: Astronomische Berechnung von Sonnenaufgang/-untergang aus GPS-Koordinaten und Datum (vereinfachte NOAA-Formel, Fehler < 2 Min). Sommer-/Winterzeit wird via `zoneinfo` korrekt berücksichtigt.
- **Dashboard-Header**: Versionsnummer `v3.0.9` wird jetzt im Titel und Footer angezeigt.

### Changed
- **`_get_dynamic_night_window()`**: Fallback komplett auf astronomische Zeiten umgestellt. Entfernt die statischen Config-Werte `night_start_hour` / `night_end_hour`. Clamps sind nun weich (±3h um Sonnenauf-/untergang) statt harter 16–21 / 6–10 Uhr.
- **`ChargeController._is_night()`**: Nutzt jetzt ebenfalls `_calculate_sun_times()` statt der statischen Config-Werte. Nacht-Erkennung passt sich somit automatisch jahreszeitlich an (z.B. 17–08h im Dezember, 21–05h im Juni).
- **Config (`config.yaml`)**: Felder `night_start_hour`, `night_end_hour` und `default_night_consumption_kwh` entfernt. Werden nicht mehr benötigt.

### Notes
- Jahreszeitliche Nachtfenster-Beispiele (Stuttgart): Dez 17–08h (15h), Jun 21–05h (8h), Mär 19–07h (12h).
- Die dynamische PV/Verbrauchs-Logik (PV < Verbrauch ab 12 Uhr / PV > Verbrauch vor 12 Uhr) bleibt erhalten und modifiziert das astronomische Fenster fein.

---
## Aeltere Changes Log gelöscht


