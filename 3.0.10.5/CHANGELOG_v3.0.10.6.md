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
