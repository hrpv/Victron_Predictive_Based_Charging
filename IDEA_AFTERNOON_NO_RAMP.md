# Idee: Kein Nachmittags-Hochrampen kurz vor Sonnenuntergang

Status: **umgesetzt in v3.0.14.0** (Stand 07.07.2026), Default **deaktiviert**
(`afternoon_no_ramp_enabled: false` in `config.yaml`) — siehe CHANGELOG.md.

## Vorschlag

In `controller.py`, Block "Nachmittag": ab einem gewissen Abstand vor Sonnenuntergang
kein erneutes Hochrampen des Ladestroms mehr auslösen, sondern den aktuellen Ladestrom
beibehalten (kein neuer `decide()`-Write). Ursprünglich als 2h-Fenster angedacht, siehe
Analyse unten. Teil derselben Motivation wie in `MODBUS_WRITE_WEAR.md` (weniger
Modbus-Writes zum Schutz des Cerbo-GX-Flash-Speichers), hier zusätzlich mit einer
Wirksamkeits-Annahme verknüpft: dass Hochrampen in diesem Fenster ohnehin nichts bringt.

## Ursprüngliche Begründung

PV ist in den letzten 2h vor Sonnenuntergang ohnehin zu schwach für nennenswerten
Zusatzertrag. Eine Lücke (SOC fällt in diesem Fenster unter Ziel) gilt als akzeptabel,
da kritische Fälle vom höherprioren ESS-State-11-Pfad (Notladung/Entladesperre, rampt
immer auf 50A) abgedeckt werden.

## Log-Analyse #1 (21./22.06.2026)

An beiden damals beobachteten Tagen mit Trigger ca. 2.2–2.4h vor Sonnenuntergang half
Hochfahren auf 50A NICHT — aber vermutlich nicht weil PV zu schwach war, sondern weil
der Verbrauch (Klimaanlage, Hitzeperiode) die Ladeleistung deutlich überstieg (SOC fiel
trotz 50A-Sollwert stetig). Bei einem früheren Trigger (3.65h vor SU, 21.06. 17:45)
brachte Hochfahren dagegen einen klaren SOC-Anstieg.

Die 2h-Grenze passte also bisher nur zufällig zu Hitzetagen mit hohem Abendverbrauch —
zu diesem Zeitpunkt noch NICHT bestätigt für Tage mit moderatem Verbrauch in diesem
Zeitfenster.

## Log-Analyse #2 (04.07.2026 — `battery_manager.log`, 20.06.–04.07., 13 Tage mit Nachmittag-Episoden)

- Der SOC-Abfall trotz Hochrampens auf 50A tritt NICHT nur an 2 Hitzetagen auf, sondern
  an 11 von 13 beobachteten Tagen über zwei Wochen — spricht für ein strukturelles
  Muster (allgemeiner Abendverbrauch), nicht für eine Hitze/Klimaanlagen-Anomalie.
- Deutlicher Schwellenwert in den Daten: Episoden, die **mehr als ~3.6h vor
  Sonnenuntergang** starten, bleiben SOC-stabil oder leicht positiv (PV reicht).
  Episoden **ab ~3.1h vor Sonnenuntergang bis Sonnenuntergang** zeigen einen SOC-Abfall
  von konstant **-8 bis -15%/h**, trotz vollem 50A-Ladestrom. Die ursprünglich geplante
  2h-Grenze ist also vermutlich zu eng gesetzt — die Daten legen eher **~3–3.5h vor SU**
  als Schwelle nahe.
- Da selbst das Maximum (50A) den Abfall nicht verhindert, ist die Ladeleistung in
  diesem Fenster nicht der limitierende Faktor — der Verbrauch übersteigt die
  verfügbare Gesamtleistung. Ein Verzicht auf das Hochrampen (Strom halten statt weiter
  zu rampen) würde den SOC-Verlauf daher vermutlich kaum verschlechtern.
- **Write-Ersparnis quantifiziert (korrigiert 04.07.2026):** Erste Zählung basierte
  fälschlich auf dem `[KEIN WRITE]`-Tag, das nur im Nachmittag-Block konsequent gesetzt
  wird — dadurch wurden IDLE/TRICKLE-Zeilen massiv als "Write" fehlgezählt (Ergebnis
  damals: 2107 Gesamt-Writes, Fenster-Anteil 1.9%). Korrekte Zählung anhand der
  eindeutigen Log-Zeile `Modbus WRITE MaxChargeCurrent = X A`:
  - **Gesamt echte Modbus-Writes im Log (15 Tage, 20.06.–04.07.): 185**, im Schnitt
    **~12–13.5 Writes/Tag** (13.5/Tag über die 13 vollständigen Tage).
  - Davon entfallen **37 Writes (~20%)** auf Nachmittag-Ramp-Episoden im
    ≤3.5h-vor-Sonnenuntergang-Fenster.
  - Die geplante Änderung würde also **rund ein Fünftel** aller täglichen Writes
    einsparen — deutlich mehr als ursprünglich angenommen, kein marginaler Effekt mehr.

## Verteilung aller 185 Writes nach Auslöser (04.07.2026)

| Kategorie | Writes | Anteil |
|---|---:|---:|
| Nachmittag | 50 | 27.0% |
| Ziel erreicht (Ramp-down zurück auf Idle-Strom) | 37 | 20.0% |
| Morgen (inkl. 2× Morgen-Notladung) | 31 | 16.8% |
| Optimal-Fenster (gesamt) | 35 | 18.9% |
| &nbsp;&nbsp;└ H11 | 15 | 8.1% |
| &nbsp;&nbsp;└ H15 | 10 | 5.4% |
| &nbsp;&nbsp;└ H14 | 6 | 3.2% |
| &nbsp;&nbsp;└ H12 | 3 | 1.6% |
| &nbsp;&nbsp;└ H13 | 1 | 0.5% |
| Nacht | 13 | 7.0% |
| ESS State (Notfall, State 11) | 7 | 3.8% |
| Cellbalancing | 7 | 3.8% |
| FULL_CHARGE (Vollladung fällig) | 3 | 1.6% |
| ohne eindeutigen Kontext | 2 | 1.1% |

Einordnung: Nachmittag ist der größte Einzelblock (27%), dicht gefolgt von den
"Ziel erreicht"-Ramp-downs (20% — das Gegenstück zum Hochrampen, wenn der Strom nach
Erreichen des SOC-Ziels wieder auf Idle zurückgefahren wird und von der hier
vorgeschlagenen Änderung nicht betroffen wäre). Optimal-Fenster (alle Stunden
zusammen, ~19%) und Morgen (~17%) sind ähnlich groß. FULL_CHARGE selbst verursacht
mit nur 3 Writes kaum Last, da die Vollladung meist einmal anläuft und dann stabil
bei 50A bleibt.

## Entscheidung (Stand 04.07.2026)

Änderung wird vorerst **nicht umgesetzt**, obwohl die korrigierte Write-Ersparnis
(~20% aller Writes) deutlich relevanter ist als ursprünglich berechnet. Vor einer
Umsetzung sollte der Schwellenwert (~3–3.5h statt 2h vor SU) und die SOC-Neutralität
noch anhand weiterer Tage bestätigt werden. Die Implementierung selbst gilt als
einfach, sobald die Datenlage klar ist.
