# Idee: Kein Nachmittags-Hochrampen kurz vor Sonnenuntergang

Status: **zurückgestellt, nicht umgesetzt** (Stand 04.07.2026)

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
- **Write-Ersparnis quantifiziert:** Im Beobachtungszeitraum verursachten die
  Nachmittag-Ramp-Episoden im ≤3.5h-Fenster 39 echte Modbus-Writes über 14 Episoden
  (~2.6 Writes/Tag im Schnitt). Das sind nur **~1.9% aller 2107 Writes** im
  Log-Zeitraum — hochgerechnet ~900–950 Writes/Jahr. Die Ersparnis ist real, aber
  klein; der Hauptnutzen der Änderung wäre nicht der Flash-Schutz selbst (da anderswo,
  z. B. IDLE/TRICKLE-Feinjustierung, deutlich mehr Writes anfallen), sondern dass sie
  SOC-neutral ist.

## Entscheidung (Stand 04.07.2026)

Änderung wird vorerst **nicht umgesetzt**. Der Nutzen (kleine Write-Ersparnis,
SOC-neutral) rechtfertigt aktuell nicht den Aufwand/das Risiko einer Codeänderung.
Idee bleibt für später festgehalten, falls sich die Priorität ändert oder weitere Daten
den ~3–3.5h-Schwellenwert erhärten. Die Implementierung selbst gilt als einfach, sobald
die Datenlage klar ist.
