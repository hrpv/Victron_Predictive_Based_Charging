# Modbus-Schreibzyklen und Flash-Verschleiß: Hintergrund und Designentscheidungen

Dieses Dokument fasst die Überlegungen zur Häufigkeit der Modbus-TCP-Schreibzugriffe
(`MaxChargeCurrent`, Register 2705) auf den Cerbo GX zusammen, die in die Entwicklung
dieses Projekts eingeflossen sind. Es soll nachvollziehbar machen, welche Annahmen
getroffen wurden, welche Risiken bekannt sind und welche Gegenmaßnahmen im Code
umgesetzt wurden — als Grundlage für eigene Entscheidungen bei der Nutzung oder
Anpassung dieses Projekts.

**Diese Datei ersetzt keine Herstellerangaben.** Victron veröffentlicht keine
offizielle Spezifikation zur Schreibzyklen-Lebensdauer des internen Speichers
der GX-Geräte. Alles Folgende basiert auf Quellcode-Analyse (offizielle
Open-Source-Repositories von Victron), Community-Diskussionen und allgemeiner
Embedded-Flash-/eMMC-Technik. Bei Unsicherheit gilt: eigene Recherche und
Vorsicht statt Vertrauen auf diese Zusammenfassung.

---

## 1. Wo Schreibvorgänge tatsächlich landen

- Modbus-TCP-Writes auf `MaxChargeCurrent` werden vom Cerbo GX über den
  D-Bus-Service `com.victronenergy.settings` (`localsettings`) verarbeitet.
- `localsettings` persistiert **alle** Settings in einer einzigen Datei,
  `/data/conf/settings.xml`, auf dem **internen** Speicher des GX-Geräts.
- Eine zusätzlich eingesteckte microSD-Karte/USB-Stick am Cerbo GX dient
  ausschließlich dem optionalen VRM-Logging-Puffer (Offline-Backup für die
  Cloud-Anbindung) und ist von diesem Mechanismus **nicht** betroffen.
- Der angeschlossene Multiplus II selbst persistiert den dynamisch über
  DVCC/Modbus gesetzten Ladestrom **nicht** in seinem eigenen Speicher; er
  erhält den Wert laufend über den VE.Bus und hält ihn nur im RAM. Ein
  eventueller Speicherverschleiß durch häufige Writes betrifft daher
  ausschließlich den Cerbo GX, nicht den Multiplus.

## 2. Speichertechnik und Wear-Leveling

- Der Kernel-Log eines GX-Geräts zeigt das Root-Filesystem als **ext4 auf
  einem eMMC-Block-Device** (`mmcblk1pX`) — kein rohes Flash-Dateisystem wie
  UBIFS/JFFS2.
- ext4 selbst implementiert kein Wear-Leveling. Bei eMMC übernimmt das der
  **interne Controller des eMMC-Chips** (Flash Translation Layer), der die
  physische NAND-Adressierung von der logischen Block-Adresse trennt — ein
  Industriestandard bei eMMC/SD/SSD, unabhängig vom darüberliegenden
  Dateisystem.
- Das genaue eMMC-Modell/-Hersteller des verbauten Chips ist von Victron
  nicht öffentlich spezifiziert und kann sich zwischen Cerbo-GX-Hardware-
  Revisionen unterscheiden.

## 3. Schreibverhalten von `localsettings`

Quellcode-Analyse von [`victronenergy/localsettings`](https://github.com/victronenergy/localsettings)
(`localsettings.py`):

- Jede Werteänderung (`SetValue`) markiert die Einstellung sofort im RAM als
  geändert und startet einen Timer (`startTimeoutSaveSettings`).
- Der Timer läuft mit `timeoutSaveSettingsTime = 2` Sekunden. Trifft innerhalb
  dieses Fensters eine weitere Änderung ein, wird der Timer **nicht** erneut
  gestartet — mehrere Änderungen innerhalb desselben 2-Sekunden-Fensters
  werden zu **einem** physischen Schreibvorgang zusammengefasst
  (`writeToXmlFile`, inkl. `fsync`).
- Bei einem Steuerungsintervall von 60 Sekunden (wie in diesem Projekt) liegt
  praktisch jeder Modbus-Write außerhalb dieses 2-Sekunden-Fensters — das
  Coalescing greift in der Praxis **nicht**. Jeder Write erzeugt einen
  eigenständigen Flash-Schreibvorgang.
- `writeToXmlFile()` schreibt bei jeder Änderung die **komplette**
  `settings.xml` neu (nicht nur das geänderte Feld), atomar über
  `rename()` nach vorherigem `fsync`.

**Konsequenz für dieses Projekt:** Die Häufigkeit der eigenen Modbus-Writes
hat direkten Einfluss auf die Zahl der Flash-Schreibvorgänge auf dem Cerbo —
es gibt keinen verlässlichen internen Schutzmechanismus, auf den man sich
verlassen könnte, außer dem (bei diesem Steuerungsintervall wirkungslosen)
2-Sekunden-Debounce.

## 4. Abschätzung der Lebensdauer

Zwei Rechenwege wurden betrachtet:

**a) Naive Zyklenrechnung** (Faustregel "10.000–100.000 P/E-Zyklen pro Zelle",
oft in Community-Diskussionen zitiert): Bei z. B. 30 Writes/Tag ergibt sich
bei 100.000 Zyklen eine Lebensdauer in der Größenordnung von ~9 Jahren (~13
Jahre unter Berücksichtigung einer mehrmonatigen Winterpause ohne Aktivität).
Diese Rechnung ignoriert jedoch Wear-Leveling vollständig und unterstellt,
dass jeder Write dieselbe physische Zelle träfe — das ist genau, was
Wear-Leveling verhindern soll. Diese Abschätzung ist daher als
**Worst-Case-Untergrenze** zu verstehen, nicht als realistische Erwartung.

**b) TBW-basierte Abschätzung** (Total Bytes Written, eine reale
Herstellerspezifikation, die Wear-Leveling und Write-Amplification bereits
einrechnet): `settings.xml` ist eine kleine Text-XML-Datei (geschätzt
niedrige zweistellige KB). Bei vollständigem Neuschreiben pro Write und z. B.
30 Writes/Tag ergibt sich ein jährliches Schreibvolumen in der Größenordnung
von ~100 MB. Selbst bei konservativen TBW-Werten kleiner eMMC-Module (untere
Industrie-Spanne) liegt die daraus abgeleitete Lebensdauer um Größenordnungen
über der naiven Zyklenrechnung.

**Einordnung:** Die tatsächliche Lebensdauer liegt vermutlich deutlich über
der pessimistischen 9-13-Jahre-Schätzung, lässt sich aber ohne Kenntnis des
exakten eMMC-Bausteins nicht seriös auf eine einzelne Zahl festlegen. Genau
deshalb wurde im Projekt der pragmatische Weg gewählt: die Schreibfrequenz so
weit senken, wie es ohne Funktionsverlust möglich ist, statt sich auf eine
unsichere Lebensdauer-Prognose zu verlassen.

## 5. Im Code umgesetzte Gegenmaßnahmen

| Maßnahme | Wirkung |
|---|---|
| Hysterese (`min_charge_duration_minutes`, 1 A Mindestabweichung) | Kein Write, wenn sich der Zielwert nicht relevant ändert |
| Stromrampe (`current_ramp_step`) | Begrenzt Schrittgröße pro Zyklus — bewusst **nicht** auf das Minimum gesetzt, sondern groß genug gewählt, um die Zahl der Zwischenschritte pro Sollwertänderung zu reduzieren, ohne die Reaktion auf reale PV-Schwankungen tagsüber zu verschlechtern |
| Direktsprung bei Nacht (`_ramp`, Sonnenauf-/untergangs-Erkennung) | Vor Sonnenaufgang/nach Sonnenuntergang wird der Zielwert direkt gesetzt statt schrittweise angefahren, da die tatsächlich fließende Leistung in dieser Phase ohnehin durch die PV-Erzeugung (nahe 0) begrenzt ist — ein Rampen-Zwischenschritt hätte keinen Effekt auf den realen Ladestrom, würde aber zusätzliche Writes erzeugen |
| Einmaliger Write bei Sondermodi (z. B. Winterpause) | Ein Flag verhindert wiederholte identische Writes über viele Zyklen hinweg |

Diese Maßnahmen wurden eingeführt, nachdem Logauswertungen zeigten, dass
Übergänge zwischen Lademodi (z. B. FULL_CHARGE → Trickle, oder der Übergang
in/aus der Nachtphase) ohne diese Vorkehrungen mehrere aufeinanderfolgende
Writes innerhalb wenig Minuten erzeugten, ohne dass diese Zwischenschritte
einen messbaren Nutzen für den tatsächlich fließenden Ladestrom hatten.

## 6. Bekannte Grenzen dieser Analyse

- Keine offizielle Bestätigung von Victron zu eMMC-Modell, TBW-Spezifikation
  oder Wear-Leveling-Implementierung des konkreten Cerbo-GX-Geräts.
- Die hier zitierten Zyklen-/TBW-Werte sind allgemeine Branchengrößen, keine
  geräte-spezifischen Messwerte.
- Andere GX-Geräte-Generationen oder -Varianten könnten abweichende Hardware
  (andere eMMC-Chips, andere Partitionsgrößen) verwenden.
- Diese Einschätzung wurde mit Unterstützung von Claude (Anthropic) erstellt
  und ist keine professionelle Hardware- oder Herstellerauskunft.

## 7. Praktische Empfehlung für Nutzer dieses Projekts

- `current_ramp_step` in `config.yaml` nicht auf einen sehr kleinen Wert
  setzen, wenn Schreibhäufigkeit eine Rolle spielen soll — ein gröberer
  Wert (z. B. 15–25 A) reduziert die Zahl der Zwischenschritte deutlich,
  ohne die Regelgüte bei realen PV-Schwankungen relevant zu verschlechtern.
- Wer die eigene Schreibfrequenz beobachten möchte:
  ```bash
  journalctl -au solar-battery --since today | grep -c "Modbus WRITE"
  ```
- Dieses Projekt erhebt keinen Anspruch darauf, jede denkbare Ursache für
  übermäßige Schreibzugriffe ausgeschlossen zu haben. Wer den Code anpasst
  oder das Steuerungsintervall (`control_interval_seconds`) deutlich
  verkürzt, sollte die Schreibhäufigkeit erneut prüfen.

---

*Dieses Dokument wurde im Rahmen der laufenden Entwicklung erstellt, um die
Auseinandersetzung mit dem Thema Flash-Verschleiß nachvollziehbar zu machen.
Es stellt keine Garantie und keine Haftungsübernahme dar. Nutzung dieses
Projekts erfolgt wie bei privater Open-Source-Software üblich auf eigene
Verantwortung *
