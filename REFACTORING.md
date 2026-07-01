# Refactoring-Dokumentation: battery_manager.py → Modulstruktur

**Datum:** 2026-06-12  
**Versionsbereich:** v3.0.9.28 → v3.0.10.5  
**Ziel:** Monolithische 3229-Zeilen-Datei in wartbare, separat analysierbare Module aufteilen

---

## Ausgangslage

```
battery_manager_v3.0.9.28.py    3229 Zeilen  (eine Datei)
```

Die Datei enthielt in dieser Reihenfolge:
1. Header-Docstring mit Register-Referenz (~62 Zeilen)
2. Changelogs als eingebettete `"""..."""`-Blöcke (~310 Zeilen)
3. Imports + Dataclasses + alle Klassen + Ladelogik (~2300 Zeilen)
4. `DASHBOARD_HTML` als Raw-String + Flask-Server (~570 Zeilen)

**Problem:** Bei jeder KI-Debugging-Sitzung musste die gesamte Datei hochgeladen
werden. Für den häufigsten Fall (Ladelogik-Bug in `ChargeController`) waren
~2800 der 3229 Zeilen irrelevant.

---

## Resultierende Modulstruktur

```
solar_battery/
├── battery_manager.py    360 Zeilen   Einstiegspunkt, Glue-Code
├── controller.py        1232 Zeilen   Ladelogik (häufigste Änderungen)
├── dashboard.py          434 Zeilen   HTML-Template, Flask, Heartbeat
├── forecast.py           464 Zeilen   VRM / Open-Meteo / Solcast / Dummy
├── modbus_victron.py     255 Zeilen   Modbus-TCP, Register-Mapping
├── logging_setup.py      191 Zeilen   DeduplicatingFilter, setup_logging
├── evcc.py               128 Zeilen   evcc REST + Reg-2901-Überwachung
├── models.py             107 Zeilen   Dataclasses (reine Datenmodelle)
├── CHANGELOG.md                       Versionshistorie (aus Code entfernt)
└── config.yaml                        (unverändert)
                         ─────────────
Total                    3171 Zeilen   (verteilt auf 8 Module)
```

**Reduktion Hauptdatei:** 3229 → 360 Zeilen (−89 %)

---

## Schritte im Detail

### Schritt 0 — Vorbereitung: dashboard.py + CHANGELOG.md (v3.0.10.0)

**Was:** `DASHBOARD_HTML`, `_HeartbeatThread`, `start_dashboard()` ausgelagert.
Changelogs aus dem Python-Quellcode in eine eigene Markdown-Datei überführt.

**Warum zuerst:** Größter sofortiger Gewinn ohne jedes Risiko —
HTML-Template und Changelogs haben null Abhängigkeiten zur Ladelogik.

**Wichtigste Änderung:**
```python
# Vorher: hardcoded an 4 Stellen
"Solar Batterie Manager v3.0.9.28"

# Nachher: eine einzige Konstante
VERSION = "3.0.10.0"
# dashboard.py ersetzt __VERSION__ per .replace() — analog zu __REFRESH__ und __CAP__
```

**Pitfall vermieden:** `DASHBOARD_HTML` ist ein `r"""..."""` Raw-String —
f-String-Formatierung funktioniert nicht direkt. Lösung: `.replace("__VERSION__", VERSION)`.

---

### Schritt 1 — models.py (v3.0.10.1)

**Was:** Die drei reinen Dataclasses ausgelagert:
- `SystemState` — aktueller Systemzustand (wird per `asdict()` ans Dashboard geliefert)
- `HourlyForecast` — stündliche PV-Prognose
- `HourlyHistory` — tatsächlicher Stundenverlauf (History-Ringpuffer)

**Bewusst NICHT in models.py:**
- `EnergyAccumulator` — hat `update()`/`reset()`-Logik → kein reines Datenmodell
- `PowerSmoother` — hat Glättungslogik → gehört zur Steuerung

**Warum zuerst unter den Klassen:** `models.py` hat keine Projektabhängigkeiten
(nur `dataclasses` aus stdlib). Alle anderen Module importieren daraus.
Sobald `models.py` existiert, können alle weiteren Module sauber
`from models import ...` verwenden ohne zirkuläre Imports zu riskieren.

```python
# Einzige Abhängigkeit in models.py:
from dataclasses import dataclass, field
```

---

### Schritt 2 — logging_setup.py (v3.0.10.2)

**Was:** `DeduplicatingFilter` und `setup_logging()` ausgelagert.

**Besonderheit:** `DeduplicatingFilter` wird in zwei Modulen gebraucht:
- `battery_manager.py` — erzeugt die Instanzen via `setup_logging()`
- `dashboard.py` — erzeugt eine eigene Instanz für Werkzeug-Access-Logs

`dashboard.py` holt die Klasse zur Laufzeit per `type(dedup_stream)` —
kein direkter Import nötig, kein zirkulärer Import möglich.

**Nebeneffekt:** `import re` konnte aus `battery_manager.py` entfernt werden
(nur noch in `logging_setup.py` für HTTP-Access-Log-Normalisierung gebraucht).

---

### Schritt 3 — forecast.py (v3.0.10.3)

**Was:** `VrmForecastManager` und `ForecastManager` ausgelagert (~450 Zeilen).
Größter einzelner Gewinn.

**Einzige Projektabhängigkeit:**
```python
from models import HourlyForecast
```

**Wichtig:** `_calculate_sun_times()` bleibt eine Methode von `ForecastManager`
(nicht umbenannt). `ChargeController` greift über `self.forecast._calculate_sun_times()`
darauf zu — das ist eine gewollte enge Kopplung, kein Fehler.

**Nebeneffekt:** `import math` bleibt (noch) in `battery_manager.py`,
weil `ChargeController._is_night()` `math.ceil`/`math.floor` verwendet
(wird in Schritt 5 mit dem Controller mitgenommen).

---

### Schritt 4 — modbus_victron.py + evcc.py (v3.0.10.4)

**Was:** Beide parallel ausgelagert — keine Abhängigkeit zwischen ihnen.

```
modbus_victron.py  →  from models import SystemState (TYPE_CHECKING)
evcc.py            →  from models import SystemState (TYPE_CHECKING)
                       from modbus_victron import VictronModbus (TYPE_CHECKING)
```

**TYPE_CHECKING-Pattern:**
```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from models import SystemState        # nur für Typ-Checker, nie zur Laufzeit
    from modbus_victron import VictronModbus
```
Zur Laufzeit werden `SystemState` und `VictronModbus` als Parameter übergeben —
kein echter Import nötig, kein zirkulärer Import möglich.

**Nebeneffekt:** Der pymodbus try/except-Fallback-Block (3.x / 2.x) konnte
vollständig aus `battery_manager.py` entfernt werden.

---

### Schritt 5 — controller.py (v3.0.10.5)

**Was:** `EnergyAccumulator`, `PowerSmoother`, `ChargeController` ausgelagert
(~1190 Zeilen). Der Kern des Systems.

**Abhängigkeiten von controller.py:**
```python
# Laufzeit-Imports (echte Abhängigkeiten):
from models import SystemState, HourlyForecast, HourlyHistory
from forecast import ForecastManager

# TYPE_CHECKING-only (nur für Typ-Checker):
from modbus_victron import VictronModbus
from evcc import EvccMonitor
```

**Kritischer Bugfix:** `from __future__ import annotations` war zwingend nötig.

**Ursache des Bugs:**
```python
# ChargeController.__init__() Signatur in controller.py:
def __init__(self, cfg, state: SystemState,
             forecast: ForecastManager, victron: VictronModbus,  # <- Problem
             evcc: EvccMonitor, logger):
```
Python wertet Typ-Annotationen in `__init__`-Signaturen standardmäßig zur
Laufzeit aus. `VictronModbus` und `EvccMonitor` standen nur im
`TYPE_CHECKING`-Block → zur Laufzeit undefiniert → `NameError`.

**Fix:**
```python
# Zeile 3 in controller.py:
from __future__ import annotations
```
PEP 563: alle Annotationen im Modul werden lazy als Strings behandelt,
nie zur Laufzeit ausgewertet. Gilt für Python 3.7+.

**Symptom im Journal:**
```
NameError: name 'VictronModbus' is not defined
  File "controller.py", line 137, in ChargeController
    forecast: ForecastManager, victron: VictronModbus,
```

---

## Import-Graph (Abhängigkeiten zur Laufzeit)

```
battery_manager.py
  ├── models.py              (keine Projektabhängigkeiten)
  ├── logging_setup.py       (keine Projektabhängigkeiten)
  ├── modbus_victron.py      (keine Projektabhängigkeiten)
  ├── evcc.py
  │     └── (models, modbus_victron via TYPE_CHECKING only)
  ├── forecast.py
  │     └── models.py
  ├── controller.py
  │     ├── models.py
  │     └── forecast.py
  │     └── (modbus_victron, evcc via TYPE_CHECKING only)
  └── dashboard.py
        └── (DeduplicatingFilter via type() zur Laufzeit)
```

Keine zirkulären Abhängigkeiten. Jedes Modul kann unabhängig importiert werden.

---

## Deployment auf dem Raspberry Pi

Alle Dateien liegen im selben Verzeichnis `/home/pi/solar_battery/`.
Keine `__init__.py`, kein Python-Package — absolute Imports funktionieren
direkt, `WorkingDirectory` in der systemd `.service`-Datei muss auf
dieses Verzeichnis zeigen:

```ini
[Service]
WorkingDirectory=/home/pi/solar_battery
ExecStart=/home/pi/solar_battery/venv/bin/python3 battery_manager.py config.yaml
```

---

## KI-Debugging-Anleitung

| Fehlertyp | Hochzuladende Dateien |
|---|---|
| Ladelogik, decide(), Simulation | `controller.py` |
| Ladelogik + Prognose-Abhängigkeit | `controller.py` + `forecast.py` |
| Modbus / falsche Registerwerte | `modbus_victron.py` |
| evcc-Konflikt, Reg 2901 | `evcc.py` + `controller.py` |
| Dashboard / GUI-Anzeige | `dashboard.py` |
| Startup-Crash / Config | `battery_manager.py` + `models.py` |
| Logging / Heartbeat | `logging_setup.py` + `dashboard.py` |

In ~90 % der Fälle reicht `controller.py` allein.
