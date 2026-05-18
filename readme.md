# Solar Batterie Manager – Vollständige Dokumentation
### Prognosebasiertes Laden für LFP-Akku mit Victron Multiplus II + Cerbo GX

---

## Inhaltsverzeichnis

1. [Systemübersicht](#1-systemübersicht)
2. [Hardware & Voraussetzungen](#2-hardware--voraussetzungen)
3. [Ladelogik & Strategie](#3-ladelogik--strategie)
4. [Modbus-Register Referenz](#4-modbus-register-referenz)
5. [evcc Koordination](#5-evcc-koordination)
6. [PV-Prognose](#6-pv-prognose)
7. [Installation](#7-installation)
8. [Konfiguration](#8-konfiguration-configyaml)
9. [Web-Dashboard](#9-web-dashboard)
10. [Betrieb & Monitoring](#10-betrieb--monitoring)
11. [Fehlerbehebung](#11-fehlerbehebung)
12. [Deployment-Optionen](#12-deployment-optionen)

---

## 1. Systemübersicht

```
┌─────────────────────────────────────────────────────────┐
│                    battery_manager.py                    │
│                                                         │
│  ForecastManager   VictronModbus    EvccMonitor         │
│  (Open-Meteo API)  (Modbus TCP)     (REST API)          │
│        │                │                │              │
│        └────────────────┼────────────────┘              │
│                         │                               │
│              ChargeController                           │
│         (Ladeentscheidung 60s-Zyklus)                   │
│                         │                               │
│              Flask Dashboard :5000                      │
└─────────────────────────┼───────────────────────────────┘
                          │ Modbus TCP Port 502
                    Cerbo GX (Venus OS)
                          │
               ┌──────────┴──────────┐
          Multiplus II          MPPT / PV-WR
          (ESS, DVCC)           (AC-gekoppelt)
               │
           LFP Akku 14 kWh
```

### Datenfluß

| Quelle | Protokoll | Was |
|---|---|---|
| Cerbo GX | Modbus TCP (lesen) | SOC, Spannung, Strom, Leistung, PV, Last, Netz |
| Cerbo GX | Modbus TCP (schreiben) | MaxChargeCurrent (Reg 2705, DVCC) |
| Cerbo GX | Modbus TCP (lesen) | ESS MinSoc (Reg 2901, evcc-Erkennung) |
| Open-Meteo / Solcast | HTTPS REST | Stündliche PV-Prognose |
| evcc | HTTP REST | Lademodus, Wallbox-Leistung (optional) |
| Flask | HTTP :5000 | Web-Dashboard für Browser |

---

## 2. Hardware & Voraussetzungen

### Anlage
| Komponente | Details |
|---|---|
| Wechselrichter | Victron Multiplus II (ESS-Modus) |
| Steuereinheit | Victron Cerbo GX (Venus OS) |
| Batterie | LFP 14 kWh, 48V |
| PV-Anlage | 10 kWp, AC-gekoppelt über PV-Wechselrichter |

### Software-Voraussetzungen
| Komponente | Version |
|---|---|
| Raspberry Pi OS | Bookworm (Python 3.13) |
| Python | 3.9+ |
| python3-venv | via apt |
| Modbus TCP am Cerbo | aktiviert (Port 502) |
| DVCC am Multiplus | aktiviert |

### Victron-Einstellungen
```
Cerbo GX:
  Einstellungen → Dienste → Modbus TCP → Ein

Multiplus II / ESS:
  Einstellungen → DVCC → Ein
  DVCC → Maximaler Systemladestrom: 50A
```

---

## 3. Ladelogik & Strategie

### Ziele
- **LFP-Schonung**: Bevorzugt SOC zwischen 20–80% halten
- **Sommer-Optimierung**: Morgens nicht unnötig laden, auf PV-Überschuss warten
- **Zellbalancing**: Spätestens alle 10 Tage Vollladung auf 98%
- **Dynamisch**: Entscheidung basiert auf Echtzeit + Prognose

### Entscheidungsbaum (60-Sekunden-Zyklus)

```
┌─ SOC ≤ 25% (Notfall)?
│   └─ JA  → Sofort mit 50A laden
│
├─ evcc-Schnellladen aktiv (Reg 2901 > 25%)?
│   └─ JA  → Laden erlaubt, Min-SOC auf Reg-2901-Wert angehoben
│
├─ ≥ 10 Tage seit letzter Vollladung?
│   └─ JA  → Laden bis 98% (Zellbalancing)
│
├─ Nacht (21:00–06:00 Uhr)?
│   └─ JA  → Kein Laden
│
├─ Morgen-Fenster (06:00–10:00 Uhr)?
│   ├─ PV-Prognose heute reicht für Abend-Ziel? → Warten
│   ├─ SOC noch ausreichend?                     → Warten
│   └─ Sonst                                     → Sanft laden (5A)
│
├─ PV-Überschuss > 200W (PV > Verbrauch)?
│   └─ JA  → Laden mit Überschuss-Strom (proportional, max 50A)
│
├─ SOC deutlich unter Ziel (> 10% Abstand)?
│   └─ JA  → Sanft laden (5A Trickle)
│
└─ Ziel-SOC erreicht?
    └─ JA  → Stop (Ladestrom = 0A)
```

### Dynamisches Ladeziel

```
Projizierter Abend-SOC = aktueller SOC + PV-Restprognose heute

Wenn projizierter Abend-SOC ≥ 80%:
  → Ziel = 80% (normaler Betrieb)
Wenn projizierter Abend-SOC < 80%:
  → Ziel = 80% + (Fehlmenge × 0.5), max 98%
  → Etwas früher nachladen um Nacht abzudecken
```

### Ladestrom-Regelung
- Sanftes Rampen: ±5A pro Regelzyklus (kein abrupter Sprung)
- Bei PV-Überschuss: `Strom = PV-Überschuss [W] / 48V`
- Minimum: 0A (kein Laden), Maximum: 50A (konfigurierbar)
- Trickle-Laden: 5A (konfigurierbar)

### Vollladungs-Tracking
Datum der letzten Vollladung wird in `state.json` gespeichert und überlebt Neustarts.

---

## 4. Modbus-Register Referenz

### Wichtig: mbpoll vs. pymodbus Offset

> **mbpoll** zählt Register ab **1** (Modbus-Spec-Darstellung)
> **pymodbus** verwendet die **echte Adresse ab 0** (= mbpoll − 1)
> Victron-Dokumentation verwendet mbpoll-Notation!

### Register-Tabelle (Unit-ID 100, Cerbo GX)

| Messwert | Victron ID (pymodbus) | mbpoll-Anzeige | Typ | Skalierung |
|---|---|---|---|---|
| Batteriespannung | 840 | 841 | uint16 | ÷ 10 → V |
| Batteriestrom | 841 | 842 | int16 | ÷ 10 → A |
| Batterieleistung | 842 | 843 | int16 | direkt W |
| Batterie SOC | 843 | 844 | uint16 | direkt % |
| PV-WR L1 | 811 | 812 | uint16 | direkt W |
| PV-WR L2 | 812 | 813 | uint16 | direkt W |
| PV-WR L3 | 813 | 814 | uint16 | direkt W |
| AC-Last L1 | 817 | 818 | uint16 | direkt W |
| AC-Last L2 | 818 | 819 | uint16 | direkt W |
| AC-Last L3 | 819 | 820 | uint16 | direkt W |
| Netz L1 | 820 | 821 | int16 | direkt W |
| Netz L2 | 821 | 822 | int16 | direkt W |
| Netz L3 | 822 | 823 | int16 | direkt W |
| **ESS MinSoc** | **2901** | **2902** | **uint16** | **direkt %** |
| **DVCC MaxChargeCurrent** | **2705** | **2706** | **uint16** | **direkt A** |

> Netz: positiv = Bezug, negativ = Einspeisung
> Strom: positiv = laden, negativ = entladen

### Register manuell prüfen (mbpoll)
```bash
sudo apt install mbpoll

# SOC (sollte z.B. 89 zeigen)
mbpoll -a 100 -r 844 -c 1 192.168.178.61

# PV-Leistung L1/L2/L3
mbpoll -a 100 -r 812 -c 3 192.168.178.61

# Netz L1/L2/L3 (signed)
mbpoll -a 100 -r 821 -c 3 192.168.178.61

# ESS MinSoc (evcc-Erkennung)
mbpoll -a 100 -r 2902 -c 1 192.168.178.61

# MaxChargeCurrent schreiben (Test 10A)
mbpoll -a 100 -r 2706 -t 4 192.168.178.61 10
```

---

## 5. evcc Koordination

### Warum kein Konflikt?

evcc und battery_manager schreiben auf **unterschiedliche Register**:

| System | Register | Zweck |
|---|---|---|
| battery_manager | 2705 (MaxChargeCurrent) | Ladestrom-Limit |
| evcc | 2901 (ESS MinSoc) | Entladeschutz beim Schnellladen |

Kein Schreibkonflikt – beide arbeiten parallel.

### evcc-Verhalten beim Schnellladen

```
Normalzustand:   Reg 2901 = 10–20%  → battery_manager: normaler Betrieb
Schnellladen:    Reg 2901 ≈ SOC     → battery_manager: Min-SOC angehoben
                 (z.B. SOC=70% → Reg 2901=70%)
                 Laden weiterhin erlaubt, Entladen blockiert
Fertig geladen:  Reg 2901 = 10–20%  → battery_manager: normaler Betrieb
```

### Erkennung im Code
```python
# EvccMonitor liest Reg 2901 alle 30s via Modbus
if reg_2901_wert > 25:
    evcc_discharge_locked = True
    effective_min_soc = reg_2901_wert  # statt battery.min_soc
```

### evcc REST-API (optional, nur für Dashboard-Info)
```
GET http://evcc-host:7070/api/state
→ Zeigt Lademodus, Wallbox-Leistung im Dashboard
→ Kein Einfluss auf Ladesteuerung
```

---

## 6. PV-Prognose

### Open-Meteo (Standard, kostenlos)
- Kein API-Key, keine Registrierung
- Stündliche Globalstrahlung für Standort-Koordinaten
- Umrechnung: `PV [kWh] = Strahlung [W/m²] / 1000 × Peak [kWp] × Effizienz × (1 - Bewölkung × 0.3)`
- Aktualisierung: stündlich (konfigurierbar)

### Solcast (optional, genauer)
- Kostenlos für Privatnutzer: 10 API-Calls/Tag
- Berücksichtigt Modulausrichtung, Neigung, lokale Abschattung
- Registrierung: https://solcast.com/free-rooftop-solar-forecasting/

```yaml
forecast:
  provider: "solcast"
  solcast_api_key: "dein-api-key"
  solcast_resource_id: "deine-resource-uuid"
```

### Verwendung der Prognose
```
pv_remaining_today    → wieviel PV kommt heute noch?
night_consumption     → wieviel Strom brauchen wir heute Nacht?
projected_evening_soc → SOC-Schätzung um 21:00 Uhr
→ Entscheidet ob Morgen-Verzögerung greift
```

---

## 7. Installation

### Schritt 1: Voraussetzungen prüfen
```bash
python3 --version     # mind. 3.9
python3 -m venv --help || sudo apt install python3-venv
```

### Schritt 2: Dateien übertragen
```bash
mkdir -p /home/pi/solar_battery
# Dateien kopieren: battery_manager.py, config.yaml,
#                   requirements.txt, solar-battery.service, install.sh
```

### Schritt 3: Konfiguration anpassen
```bash
nano /home/pi/solar_battery/config.yaml
```

Mindestens anpassen:
```yaml
modbus:
  host: "192.168.178.61"       # IP des Cerbo GX

evcc:
  enabled: true
  api_url: "http://localhost:7070/api/state"   # oder IP wenn remote
  # enabled: false  wenn kein evcc vorhanden
```

### Schritt 4: Installation ausführen
```bash
bash /home/pi/solar_battery/install.sh
```

Das Skript macht automatisch:
1. Virtual Environment anlegen (`venv/`) – System-Python bleibt unberührt
2. Python-Pakete installieren (`pymodbus`, `flask`, `requests`, `pyyaml`)
3. Systemd-Service installieren und für Autostart aktivieren

### Schritt 5: Starten & prüfen
```bash
sudo systemctl start solar-battery
sudo systemctl status solar-battery
tail -f /home/pi/solar_battery/battery_manager.log
```

### Schritt 6: Dashboard aufrufen
```
http://<raspberry-pi-ip>:5000
```

---

## 8. Konfiguration (config.yaml)

### Vollständige Parameter-Referenz

```yaml
# ── Modbus TCP ──────────────────────────────────────────
modbus:
  host: "192.168.178.61"    # IP Cerbo GX – ANPASSEN
  port: 502                 # Standardport, nicht ändern
  unit_id: 100              # Cerbo GX Unit-ID, fest 100
  timeout_seconds: 5

# ── evcc ────────────────────────────────────────────────
evcc:
  enabled: true
  api_url: "http://localhost:7070/api/state"   # ANPASSEN
  timeout_seconds: 5
  poll_interval_seconds: 30  # Wie oft Reg 2901 + REST lesen

# ── Batterie ────────────────────────────────────────────
battery:
  capacity_kwh: 14.0         # Kapazität bei 100% SOC
  min_soc: 20                # Untere Grenze [%]
  max_soc: 98                # Obere Grenze [%] (LFP)
  target_soc_normal: 80      # Normales Ladeziel [%]
  full_charge_interval_days: 10  # Tage bis Vollladung
  min_charge_current: 0      # 0 = Laden gesperrt
  max_charge_current: 50     # Max Ladestrom [A]
  trickle_current: 5         # Sanft-Laden [A]
  voltage_nominal: 48.0      # Nennspannung [V]

# ── PV-Anlage ───────────────────────────────────────────
pv:
  peak_power_kwp: 10.0       # Anlagenleistung [kWp]
  efficiency_factor: 0.82    # Systemwirkungsgrad
  azimuth_deg: 180           # 180=Süd, 90=Ost, 270=West
  tilt_deg: 30               # Neigungswinkel [°]

# ── Standort ────────────────────────────────────────────
location:
  latitude: 48.7758          # GPS Breitengrad
  longitude: 9.1829          # GPS Längengrad
  timezone: "Europe/Berlin"

# ── Prognose ────────────────────────────────────────────
forecast:
  provider: "open_meteo"     # open_meteo | solcast
  update_interval_minutes: 60
  solcast_api_key: ""        # nur für provider=solcast
  solcast_resource_id: ""

# ── Ladesteuerung ───────────────────────────────────────
charging:
  control_interval_seconds: 60    # Regelzyklus
  soc_hysteresis: 2               # Puffer am Ziel [%]
  current_ramp_step: 5            # Strom-Rampe [A/Zyklus]
  avg_daily_consumption_kwh: 8.0  # Tagesverbrauch
  emergency_charge_soc: 25        # Notfall-SOC [%]
  night_start_hour: 21
  night_end_hour: 6
  morning_delay_start_hour: 6
  morning_delay_end_hour: 10

# ── Dashboard ───────────────────────────────────────────
dashboard:
  enabled: true
  host: "0.0.0.0"
  port: 5000
  refresh_interval_seconds: 30
  state_file: "/home/pi/solar_battery/state.json"

# ── Logging ─────────────────────────────────────────────
logging:
  level: "INFO"              # DEBUG|INFO|WARNING|ERROR
  file: "/home/pi/solar_battery/battery_manager.log"
  max_size_mb: 10
  backup_count: 3
  log_decisions: true        # Jede Entscheidung loggen
```

---

## 9. Web-Dashboard

Aufruf: `http://<pi-ip>:5000`

### Anzeigeelemente

| Karte | Inhalt |
|---|---|
| Batterie SOC | SOC%, kWh, farbiger Ladebalken |
| Batterie | Spannung [V], Strom [A] mit Richtung ↑↓, Leistung [W] |
| Lademodus | idle/charging/trickle/full_charge, Strom-Sollwert, Tage seit Vollladung |
| PV Leistung | Aktuelle Leistung [W], Energie heute [kWh] |
| Verbrauch | Aktuelle Last [W], Energie heute [kWh] |
| Netz | Aktuelle Leistung [W] (+ Bezug / − Einspeisung) |
| PV Prognose | Gesamtprognose heute [kWh], noch verbleibend |
| Nachtverbrauch | Erwarteter Verbrauch [kWh], aktuelles Ziel-SOC |

### Entscheidungsbox
Zeigt im Klartext warum gerade geladen/nicht geladen wird, z.B.:
> „Morgen-Verzögerung: PV-Prognose heute 28.4 kWh, projizierter Abend-SOC 87% ≥ Ziel 80%. Kein Laden nötig."

### Tagesgrafik
Balkendiagramm: PV-Ertrag vs. Verbrauch pro Stunde, aktuelle Stunde hervorgehoben.

### Ladeplan-Tabelle
Stündliche Vorschau mit projiziertem SOC-Verlauf, Aktion und geplantem Ladestrom.

---

## 10. Betrieb & Monitoring

### Systemd-Befehle
```bash
sudo systemctl start solar-battery      # Starten
sudo systemctl stop solar-battery       # Stoppen
sudo systemctl restart solar-battery    # Neu starten
sudo systemctl status solar-battery     # Status
sudo systemctl enable solar-battery     # Autostart an
sudo systemctl disable solar-battery    # Autostart aus
```

### Log-Monitoring
```bash
# Live-Log
tail -f /home/pi/solar_battery/battery_manager.log

# Systemd-Journal
journalctl -u solar-battery -f

# Letzte 100 Zeilen
tail -100 /home/pi/solar_battery/battery_manager.log

# Nur Entscheidungen
grep "CHARGING\|TRICKLE\|IDLE\|FULL\|NOTFALL" battery_manager.log
```

### Typische Log-Ausgaben
```
[INFO]  Prognose (open_meteo): 28.4 kWh heute
[INFO]  Modbus TCP: 192.168.178.61:502
[INFO]  [IDLE] 0A | Morgen-Verzögerung: PV reicht...
[INFO]  [CHARGING] 23A | PV-Überschuss: 1120W → 23A
[INFO]  [TRICKLE] 5A | SOC 45% weit unter Ziel 80%
[INFO]  [FULL_CHARGE] 50A | Vollladung: 8 Tage seit letzter
[INFO]  Vollladung erreicht (98.1%), Balancing abgeschlossen
```

### Persistenter Zustand
```bash
# Wann war die letzte Vollladung?
cat /home/pi/solar_battery/state.json
```

---

## 11. Fehlerbehebung

### Modbus-Verbindung schlägt fehl
```bash
# Erreichbarkeit
ping 192.168.178.61
nc -zv 192.168.178.61 502

# Modbus TCP aktivieren
# Cerbo GX: Einstellungen → Dienste → Modbus TCP → Ein
```

### Falscher SOC / falsche Werte
```bash
# Rohwerte direkt lesen (mbpoll = pymodbus +1)
sudo apt install mbpoll
mbpoll -a 100 -r 844 -c 1 192.168.178.61   # SOC
mbpoll -a 100 -r 841 -c 1 192.168.178.61   # Spannung (/10 → V)
mbpoll -a 100 -r 812 -c 3 192.168.178.61   # PV L1/L2/L3
mbpoll -a 100 -r 818 -c 3 192.168.178.61   # Last L1/L2/L3
mbpoll -a 100 -r 821 -c 3 192.168.178.61   # Netz L1/L2/L3
```

### Ladestrom wird nicht gesetzt
```bash
# DVCC-Register manuell schreiben (Test: 10A)
mbpoll -a 100 -r 2706 -t 4 192.168.178.61 10

# Prüfen ob DVCC aktiv
# Cerbo GX: Einstellungen → DVCC → Ein
# Victron VRM: zeigt "Externe Steuerung" wenn DVCC aktiv
```

### Werte doppelt im Log
```bash
# Ist ein alter Prozess noch aktiv?
ps aux | grep battery_manager
sudo systemctl restart solar-battery
```

### evcc-Verbindung schlägt fehl
```bash
curl http://localhost:7070/api/state | python3 -m json.tool | grep -E "mode|charging"
# Falls nicht localhost:
curl http://192.168.178.58:7070/api/state
```

### Service startet nicht
```bash
# Detaillierten Fehler anzeigen
journalctl -u solar-battery -n 50
tail -50 /home/pi/solar_battery/battery_manager.log

# Manuell testen
source /home/pi/solar_battery/venv/bin/activate
python3 battery_manager.py config.yaml
```

### Virtual Environment neu erstellen
```bash
sudo systemctl stop solar-battery
rm -rf /home/pi/solar_battery/venv
bash /home/pi/solar_battery/install.sh
sudo systemctl start solar-battery
```

---

## 12. Deployment-Optionen

### Option A: Eigener Raspberry Pi (aktuell, Entwicklungssetup)
```
Raspi (battery_manager) ──WireGuard──► Fritzbox ──Internet──► Fritzbox ──► Cerbo GX
192.168.168.54:5000                                            192.168.178.x   .61
```
- config.yaml bleibt wie ist
- WireGuard-Tunnel für Modbus und evcc-API nötig

### Option B: Gleicher Raspi wie evcc (empfohlen für Produktion)
```
Raspi (evcc + battery_manager) ──LAN──► Cerbo GX
192.168.178.58                          192.168.178.61
```
Änderungen in config.yaml:
```yaml
modbus:
  host: "192.168.178.61"    # unverändert

evcc:
  api_url: "http://localhost:7070/api/state"   # localhost statt IP
```
- Kein WireGuard nötig
- Beide Dienste laufen parallel, kein Ressourcenkonflikt (Go + Python)
- Raspi 3b mit 1GB RAM reicht für beide

### Ressourcenverbrauch (Raspi 3b)
```bash
# Prüfen nach Inbetriebnahme beider Dienste
free -h
top -b -n1 | grep -E "evcc|python"
```

---

## Anhang: Dateiübersicht

| Datei | Zweck |
|---|---|
| `battery_manager.py` | Hauptskript (~1200 Zeilen) |
| `config.yaml` | Alle Einstellungen |
| `requirements.txt` | Python-Abhängigkeiten |
| `install.sh` | Automatisches Installationsskript |
| `solar-battery.service` | Systemd-Service-Definition |
| `state.json` | Persistenter Zustand (auto-generiert) |
| `battery_manager.log` | Laufendes Log (auto-generiert) |

## Anhang: Python-Abhängigkeiten

| Paket | Version | Zweck |
|---|---|---|
| pymodbus | ≥ 3.6 | Modbus TCP Client |
| flask | ≥ 3.0 | Web-Dashboard |
| requests | ≥ 2.31 | HTTP für Prognose-API + evcc |
| pyyaml | ≥ 6.0 | config.yaml parsen |

## Anhang: Victron Modbus-Dokumentation

Offizielle Register-Tabelle:
https://www.victronenergy.com/upload/documents/CCGX-Modbus-TCP-register-list-3.71.xlsx

---

*Erstellt: Mai 2026 | Getestet mit: Victron Cerbo GX Venus OS, Raspberry Pi OS Bookworm, pymodbus 3.13*

---

## 13. VRM Forecast API

### Hintergrund

Victron nutzt für die Prognose **Solcast-Satellitendaten** kombiniert mit einem eigenen Machine-Learning-Modell, das auf der historischen Produktions- und Verbrauchshistorie der eigenen Anlage trainiert wurde. Das Ergebnis ist deutlich genauer als generische Wetterdaten.

Quellen:
- Victron Blog: https://www.victronenergy.com/blog/2023/07/05/new-vrm-solar-production-forecast-feature/
- VRM API Docs: https://vrm-api-docs.victronenergy.com/
- Ähnliches Projekt (Node-RED): https://github.com/hrpv/Victron_Predictive_Based_Charging

### Voraussetzungen

| Bedingung | Details |
|---|---|
| VRM-Registrierung | Anlage muss in VRM registriert sein |
| Mindest-Historie | mind. 30 Tage Ertragsdaten in VRM |
| Standort gesetzt | GPS-Koordinaten in VRM hinterlegt |
| AC-gekoppelter PV-WR | wird vollständig unterstützt |

### Einrichtung

**1. Access Token erstellen**
```
https://vrm.victronenergy.com/access-tokens
→ "Add Token" → Name: battery_manager → kein Ablaufdatum → Create
→ Token sofort kopieren (wird nur einmal angezeigt!)
```

**2. Installation ID ermitteln**
```
VRM Portal → Einstellungen → Allgemein
→ "VRM-Installations-ID" (z.B. 318602)
```

**3. config.yaml**
```yaml
vrm:
  enabled: true
  access_token: "dein-token"   # geheim halten, nicht in Git!
  installation_id: "318602"
  timeout_seconds: 10
```

### API-Endpunkt

```
GET https://vrmapi.victronenergy.com/v2/installations/{id}/stats
    ?type=forecast
    &start={unix_timestamp_jetzt - 60s}
    &end={unix_timestamp_sonnenuntergang}
    &interval=hours

Header: x-authorization: Token {access_token}
```

### API-Antwort

```json
{
  "success": true,
  "records": {
    "solar_yield_forecast": [
      [1690675200000, 870.39],   // [unix_ms, Wh]
      [1690678800000, 2540.12],
      ...
    ],
    "vrm_consumption_fc": [
      [1690675200000, 320.5],
      ...
    ]
  },
  "totals": {
    "solar_yield_forecast": 26249.63,   // Wh gesamt
    "vrm_consumption_fc":   14065.60
  }
}
```

Wichtige Details:
- Zeitstempel sind **Unix-Millisekunden** (÷ 1000 für Python `datetime.fromtimestamp()`)
- Werte sind **Wh pro Stunde** (÷ 1000 → kWh)
- Abfrage von `jetzt − 60s` bis Sonnenuntergang liefert den **Restwert heute**
- Verbrauchsprognose (`vrm_consumption_fc`) basiert auf historischem Verbrauchsmuster
- Solar-Prognose für AC-gekoppelten PV-Wechselrichter: Feld `solar_yield_forecast` (Summe aller Quellen)

### Rollierende Tagesprognose

Die Strategie aus der Community (bewährt, Genauigkeit ±1–2 kWh):

```python
# Abfrage: (jetzt - 60s) bis Sonnenuntergang
start = int(time.time()) - 60
end   = heute_21_uhr_unix

# Ergebnis = verbleibende PV für heute
# Gesamtprognose = bereits_erzeugt_heute + verbleibend
```

Im battery_manager ist das in `VrmForecastManager.fetch()` implementiert.

### Fallback-Kette

```
VRM API verfügbar?
  JA  → VRM-Prognose verwenden (beste Qualität)
  NEIN → Solcast (falls konfiguriert)
       → Open-Meteo (immer verfügbar, kein Key)
       → Dummy-Profil (Gauss-Kurve als Notfall)
```

Dashboard zeigt die aktive Quelle:
- **VRM ★** – VRM API aktiv
- **Solcast** – Solcast API aktiv
- **Open-Meteo** – generische Wetterprognose
- **Dummy ⚠️** – kein Internet, Notfall-Profil

### Genauigkeit & Einschränkungen

- Prognose ist tagesgenau für **heute** (rollierend)
- Morgen-Prognose (next-day): in VRM-Portal sichtbar, aber API liefert nur 24–48h
- Bei **vollständig geladenem Akku** kann VRM die Erzeugung unterschätzen (Feed-in beschränkt den Ertrag)
- Neue Anlagen: bis zu 48h warten bis Prognose verfügbar ist
- Token hat vollen VRM-Zugriff → **niemals in Git einchecken**

### Verbrauchsprognose

`vrm_consumption_fc` ist besonders nützlich für die Nacht-Schätzung:
```
Tagessumme Verbrauch [Wh] ÷ 24h × Nachtstunden (21–6 Uhr = 9h)
→ Prognose nächtlicher Verbrauch
```

Damit wird `avg_daily_consumption_kwh` in `config.yaml` nur noch als Fallback genutzt, wenn VRM nicht verfügbar ist.
