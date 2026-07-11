# Changelog — Solar Batterie Manager

Victron ESS / Multiplus II + Cerbo GX | Modbus TCP | Predictive Charging
---

## v3.0.14.3 — Fix: Projizierter SOC in `_simulate_hour()` fror nachts bei kleinem Defizit ein (2026-07-09)

Symptom (Dashboard-PDF, Ladeplan-Tabelle): In der letzten simulierten Stunde
(23:00) sank der projizierte SOC trotz negativem Überschuss nicht. Bei
genauerem Hinsehen auch: SOC stieg von 20:00 auf 21:00 sogar leicht an
(83.5% -> 84.2%), obwohl beide Stunden als "ENTLADEN" markiert waren.

Root Cause: `_apply_deficit()` rechnet in jeder Entladestunde pauschal einen
`trickle_kwh`-Beitrag (`min_charge_a * nom_v`, 3A×48V = 0.144 kWh/h) gegen
das Defizit, unabhängig davon, ob `fc.pv_kwh` in dieser Stunde überhaupt
etwas liefert. Nachts ist PV=0 - dennoch floss der fiktive Trickle-Betrag
in die Rechnung ein. Bei Defiziten kleiner als 0.144 kWh (z.B. nachts bei
niedrigem Verbrauch) überstieg der fiktive Ladebeitrag das reale Defizit,
wodurch der SOC in der Simulation einfror oder sogar stieg - obwohl der
reale Controller nachts nie lädt (`decide()` Block 4: "Nacht: kein Laden",
0A, vgl. Log vom 08./09.07., wo der SOC nachts durchgängig sinkt).

Fix: Trickle-Beitrag jetzt auf das tatsächlich verfügbare PV der Stunde
gedeckelt (`min(trickle_kwh, fc.pv_kwh)`). Nachts (PV=0) reduziert sich das
automatisch auf reinen Entladepfad, keine explizite Nacht-Abfrage nötig.

---

## v3.0.14.2 — Fix: Unnötiger Full-Ramp-Spike beim Übergang Optimal-Fenster → Nachmittag (2026-07-09)

Symptom (Log 09.07.2026):
```
16:01:00  Modbus WRITE 27A  | Nachmittag: SOC 78.0% < Ziel 78% -> lade mit 50A
16:02:01  Modbus WRITE  7A  | Ziel 78% erreicht (SOC 79.0%)
16:03:01  Modbus WRITE  3A  | Ziel 78% erreicht (SOC 79.0%)
```
Drei Writes und ein kurzer Strom-Spike auf 27A für ein SOC-Defizit von
deutlich unter 1 Prozentpunkt.

Root Cause: `dyn_target` ist intern ein Float (z.B. 78.3%), das Log rundet
für die Anzeige auf ganze Prozent ("Ziel 78%"). Block 6 "Nachmittag" prüfte
bisher ungebremst `soc < dyn_target` und löste dafür sofort vollen Ramp auf
`max_a` aus — auch wenn die Differenz nur im Nachkommastellenbereich lag.
Eine Aktualisierung des SOC-Werts (hier: Sprung von 78.0% auf 79.0% durch die
eine Ladeperiode) reichte bereits, um das Ziel zu erreichen und den Ramp
sofort wieder zu kassieren.

Der Rest des Codes folgt dafür bereits einem dokumentierten Muster
("Asymmetrische Hysterese", siehe Kommentar bei "Ziel bereits erreicht"):
Abschalten bei `soc >= dyn_target`, aber Nachladen erst unterhalb von
`dyn_target - soc_hysteresis` (im Morgen-Block bereits so umgesetzt). Block 6
wurde bei seiner Einführung in v3.0.14.0 ohne diese Marge gebaut.

Fix: `if soc < dyn_target:` → `if soc < dyn_target - hyst:` in Block 6,
konsistent mit dem bestehenden Hysterese-Muster.

Trade-off (bewusst in Kauf genommen, ggf. anzupassen): Ein Rest-Defizit von
bis zu `soc_hysteresis` (Default 2%) wird am Nachmittag jetzt nicht mehr
aktiv nachgeladen, sondern fällt auf Block 7 ("Warten auf PV-Überschuss")
durch. Das Sicherheitsnetz (Notladung bei `emergency_charge_soc`, Block 2)
bleibt davon unberührt.

---

## v3.0.14.1 — Fix: `afternoon_no_ramp` griff nicht mehr direkt nach Sonnenuntergang (2026-07-08)

Symptom (Log 08.07.2026): Um 21:50 Uhr Modbus-Write auf `MaxChargeCurrent = 50 A`
trotz aktiviertem `afternoon_no_ramp_enabled`, 10 Minuten später (22:00) direkt
wieder zurück auf 3A ("Nacht: kein Laden").

Root Cause: Zwei unterschiedliche Zeitbegriffe für "Nacht" liefen auseinander.
`_is_night()` rundet den Nachtbeginn auf volle Stunden auf (`math.ceil(sunset)`);
bei Sonnenuntergang 21.3h begann "Nacht" für den Controller damit erst um 22:00,
nicht um 21:18. Block 6 ("Nachmittag") rechnet dagegen mit der präzisen
Dezimalzeit (`before_sunset_h = sunset - h_now_dec`) und verlangte zusätzlich
`before_sunset_h >= 0.0`. Im Rest-Fenster zwischen dem tatsächlichen
Sonnenuntergang und dem aufgerundeten `_is_night()`-Zeitpunkt (< 1h) wurde
`before_sunset_h` negativ, die Bedingung schlug fehl, und `decide()` fiel
zurück auf den alten Zweig (volles Rampen auf `max_a`) — exakt in dem Fenster,
das die Funktion eigentlich abdecken soll.

Fix: Untergrenze `0.0 <=` in Block 6 entfernt, Prüfung jetzt nur noch
`before_sunset_h <= threshold_h`. Die Lücke ist durch `ceil()` auf < 1h
begrenzt; sobald `_is_night()` (Block 1) tatsächlich greift, gibt `decide()`
ohnehin schon vorher zurück, eine offene Untergrenze ist hier unkritisch.

Nicht geändert: Die grundsätzliche Diskrepanz zwischen `_is_night()`
(Stunden-Rundung) und der präzisen Dezimalzeit an anderen Stellen im Code
besteht weiter fort und wurde hier nicht angefasst — siehe TODO.

---

## v3.0.14.0 — Neues Feature: Kein Hochrampen kurz vor Sonnenuntergang (2026-07-07)

Umsetzung der in `IDEA_AFTERNOON_NO_RAMP.md` zurückgestellten Idee, nachdem
die zweite Log-Analyse (04.07.2026, 13 Tage) den ursprünglich vermuteten
Effekt bestätigt hat: In den letzten ~3–3.5h vor Sonnenuntergang verhindert
selbst `max_charge_current` (50A) den SOC-Abfall durch Abendverbrauch nicht
mehr — Hochrampen bringt in diesem Fenster nichts, verursacht aber laut
Analyse rund 20% aller täglichen Modbus-Writes.

Added:
- `config.yaml`: neue Keys `afternoon_no_ramp_enabled` (Default `false`) und
  `afternoon_no_ramp_before_sunset_h` (Default `3.5`). Die Funktion ist per
  Default deaktiviert — die Schwelle ist bisher nur an einem
  Standort/Zeitraum bestätigt und muss explizit aktiviert werden.
- `controller.py` (`decide`, Block 6 "Nachmittag"): Ist
  `afternoon_no_ramp_enabled` aktiv und liegt die aktuelle Zeit innerhalb von
  `afternoon_no_ramp_before_sunset_h` vor Sonnenuntergang, wird bei
  `soc < dyn_target` nicht mehr auf `max_a` hochgerampt. Stattdessen liefert
  `decide()` `(-1, "afternoon_hold", ...)` zurück — dieselbe "kein
  Write"-Konvention wie bei `winter_pause` — und `run_cycle()` überspringt
  den Schreib-Block komplett, der zuletzt geschriebene Ladestrom bleibt
  unverändert aktiv. Außerhalb dieses Fensters (oder bei deaktiviertem
  Feature) bleibt das bisherige Verhalten (Rampe auf `max_a`) unverändert.
- Neuer `charge_mode`-Wert `afternoon_hold` fürs Dashboard/Logging, um diesen
  Fall von normalem `charging` zu unterscheiden.

Nicht geändert: `_simulate_hour()` / `build_schedule()` (Ladeplan-Projektion)
bilden dieses Verhalten noch nicht ab — die Simulation zeigt weiterhin
Hochrampen auf `max_a` im Nachmittagsfenster, unabhängig vom neuen Flag.

---

## v3.0.13.5 — Fix: TypeError (datetime - None) im Auto-Reset bei abgeschlossenem Balancing (2026-07-05)

Symptom: Laufzeit-Absturz in `run_cycle()`:
`TypeError: unsupported operand type(s) for -: 'datetime.datetime' and 'NoneType'`
in `elapsed = (datetime.now() - self._soc_98_reached_at).total_seconds()`.

Root Cause: Regression aus v3.0.13.3. Der Auto-Reset-Block hatte die Form
`if _soc_98_reached_at is None and not _balancing_completed_today: <Timer starten>
else: <elapsed berechnen>`. Der `else`-Zweig setzte implizit voraus, dass
`_soc_98_reached_at` gesetzt ist — was vor 13.3 auch galt (else wurde nur bei
laufendem Timer erreicht). Mit der in 13.3 hinzugefuegten Sperre
`_balancing_completed_today` entstand ein dritter Zustand:
`_soc_98_reached_at is None` UND `_balancing_completed_today is True`. Dann ist
die if-Bedingung `False` (wegen der Sperre) und der Ablauf faellt in den
`else`-Zweig, obwohl der Timer `None` ist -> Subtraktion `datetime.now() - None`.

Ablauf im Feld (2026-07-05): Vollladungstag, SOC>=98% & U>=55V -> Auto-Reset
nach 1h setzt `days_since_full_charge = 0` und `_soc_98_reached_at = None`; der
5h-Cellbalancing-Hold laeuft danach natuerlich ab und `decide()` setzt
`_balancing_completed_today = True`. SOC/U bleiben den restlichen Tag
>=98%/55V -> jeder weitere `run_cycle` trifft exakt den dritten Zustand ->
Crash. Am 28.06. (13.3-Release) trat das nicht auf, weil `battery_voltage`
nachmittags um 55V pendelte (54.7-55.2V); bei jedem Dip unter 55V griff der
aeussere `else`-Zweig und der kritische Pfad wurde nie erreicht. Ein stabil
ueber 55V gehaltener Nachmittag legt den Bug frei.

Fixed:
- `controller.py` (`run_cycle`, Auto-Reset-Block): Verzweigung umstrukturiert.
  `elapsed` wird jetzt ausschliesslich im Zweig `_soc_98_reached_at is not None`
  berechnet (Timer laeuft). Timerstart nur noch via
  `elif not _balancing_completed_today`. Der dritte Zustand
  (`None` UND `completed`) ist ein expliziter No-op bis zum Mitternachts-Reset
  von `_balancing_completed_today`.

Verhalten sonst unveraendert: alle bisherigen Pfade (Timer laeuft / kein Timer
und heute noch nicht abgeschlossen) verhalten sich identisch; ausschliesslich
der bislang abstuerzende Zustand wird abgefangen. `decide()`, Trigger- und
Hold-Logik bleiben unberuehrt.

---

## v3.0.13.4 — Fix: Ladeplan-Simulation ignoriert nachtverbrauchsgetriebene Vollladung (2026-07-05)

Symptom: An Tagen, an denen `decide()` durchgehend `max_charge_current` (50A)
hält, weil `dyn_target` = 98% ist, zeigte der stündliche Ladeplan
(`build_schedule` / `_simulate_hour`) stattdessen die normale
Optimal-Fenster-Logik: PAUSE in den Vormittagsstunden, dann eine 20A-Rampe
(`reduced_charge_current_a`), Übergang auf 50A erst spät am Nachmittag. Die
projizierte SOC-Kurve und die Aktions-Labels passten damit nicht zur
Realsteuerung, die von Beginn an 50A anlegte.

Root Cause: `decide()` (Block 3) erzwingt `max_a`, sobald **`dyn_target >= 98.0`**
— unabhängig davon, ob dieser Zielwert aus dem Vollladungs-Intervall
(`_needs_full_charge`) ODER aus dem Nachtverbrauchspfad
(`night_cons / capacity`, gecappt auf 98%) stammt. `_simulate_hour()` gatete
seinen Vollladungs-Zweig jedoch ausschließlich auf `needs_full`, also auf das
reine **Intervall**-Kriterium (`days_since_full_charge >= full_charge_interval_days`).

An einem Tag, an dem der 98%-Zielwert allein vom Nachtverbrauch getrieben ist
(`days_since_full_charge < interval` → `needs_full == False`), fiel die
Simulation deshalb durch den Vollladungs-Zweig hindurch in die normale
adaptive Planung — während `decide()` real 50A hielt.
Konkreter Auslöser im Feld: `last_full_charge_date` 2026-06-28,
`days_since_full_charge` = 7, `full_charge_interval_days` = 10 (Intervall-Vollladung
erst am 2026-07-08 fällig), aber Nachtverbrauch 11.7 kWh / 14.0 kWh Kapazität
hob `dyn_target` bereits auf 98%. Dass die Vollladung durch den Nachtverbrauch
früher als das Intervall getriggert wird, ist gewolltes Verhalten — lediglich
die Ladeplan-Projektion zog nicht mit. Es ist dasselbe „`dyn_target` = 98 aus
Nachtverbrauch statt aus dem Intervall"-Phänomen, das bereits v3.0.13.3
zugrunde lag, hier auf den Simulationspfad übertragen.

Fixed:
- `controller.py` (`_simulate_hour`): Der Vollladungs-Zweig gatet jetzt auf
  `target_full = dyn_target >= 98.0 or needs_full` — exakt die Bedingung, die
  auch `decide()` Block 3 auslöst. `needs_full` (Intervall) ist dabei eine
  Teilmenge und bleibt referenziert.
- Übergangsschwelle von `max_soc - hyst` (typ. 96%) auf **98%**
  (`soc_sim >= 98.0`) angehoben — beseitigt denselben 2%-Früh-Übergang
  max_a → Trickle, den v3.0.13.1 bereits in `decide()` geschlossen hatte.

Bewusste Restunschärfe: Die Simulation kennt `battery_voltage` nicht und kann
den Spannungsanteil (`U >= full_charge_min_voltage`) von `decide()`s
`full_charge_complete` nicht abbilden. Der Plan kann den Übergang
max_a → Trickle daher geringfügig früher zeigen als die Realsteuerung, die
zusätzlich auf 55V wartet. Ohne Spannung in der Prognose nicht sauber lösbar.

Nicht geändert: `decide()`, `run_cycle()`, Auto-Reset-Logik und die
Trigger-/Hold-Bedingungen bleiben unverändert; der Fix betrifft ausschließlich
die Projektion des Ladeplans.

---

## v3.0.13.3 — Fix: Mehrfacher Cellbalancing-Hold am selben Tag (2026-06-28)

Symptom: Nach einem vollständig durchlaufenen Cellbalancing-Hold (10:54–12:53,
119 Minuten, Auto-Reset bereits um 11:55 erfolgt) startete die Anlage am
Nachmittag mehrfach einen kompletten neuen 119-Minuten-Hold (14:55, 14:58,
15:30 — jeweils erneut "halte 20A noch 119 min"), obwohl
`days_since_full_charge` bereits auf 0 stand und keine Vollladung mehr fällig
war.

Root Cause: `dyn_target` blieb den ganzen Tag bei 98.0, weil dieser Wert hier
nicht aus dem 10-Tage-Zellbalancing-Intervall stammte, sondern aus dem
regulären Nachtverbrauchspfad (`night_cons / capacity`, gecappt auf 98%) —
ein an diesem Tag durchgehend gültiger Normalzustand, nicht nur ein
einmaliges Morgen-Ereignis. `battery_voltage` schwankte am Nachmittag durch
PV-/Lastwechsel knapp um die 55V-Trigger-Schwelle (54.7V–55.2V). Jedes Mal,
wenn `soc >= 98% UND U >= 55.0V` erneut gleichzeitig zutraf, sah
`run_cycle()` `_soc_98_reached_at is None` (durch den vorherigen
Hysterese-Abbruch in `decide()` zurückgesetzt) und startete einen
**komplett neuen** Hold mit voller Dauer — unabhängig davon, dass an diesem
Tag bereits ein vollständiger Zyklus gelaufen war.

Fixed:
- Neue Instanzvariable `_balancing_completed_today` (bool, Reset bei
  Mitternacht zusammen mit den übrigen Balancing-Trackern).
- `controller.py` (`decide`, Trickle-Block): Läuft die Haltezeit natürlich ab
  (`hold_active` wird `False`, weil `_balancing_hold_until` erreicht ist —
  nicht weil SOC/Spannung vorzeitig unter die Hysterese-Schwelle fielen),
  wird `_balancing_completed_today = True` gesetzt. Diese Unterscheidung
  zwischen „natürlich abgelaufen" und „vorzeitig abgebrochen" ist wichtig:
  ein vorzeitiger Abbruch (Spannung/SOC bricht ein) bedeutet weiterhin
  „Vollladung bleibt fällig" und darf erneut versucht werden; ein
  natürliches Ende bedeutet „heute bereits erfolgreich abgeschlossen".
- `controller.py` (`run_cycle`, Auto-Reset-Block): Ein neuer Hold startet nur
  noch, wenn `_soc_98_reached_at is None AND not _balancing_completed_today`.
  Spätere kurze Überschreitungen der 55V-Schwelle am selben Tag lösen damit
  keinen weiteren vollen Hold mehr aus.

Nicht geändert: Auto-Reset-Logik für `days_since_full_charge` selbst, sowie
der vorzeitige Hysterese-Abbruch (führt weiterhin zu „Vollladung bleibt
fällig", kann bei echtem SOC-Abfall erneut in Block 3 münden) bleiben
unverändert.

---

## v3.0.13.2 — trickle_current: keine Reduktion bei bereits höherem Ladestrom (2026-06-28)

Umsetzung einer zuvor dokumentierten, aber noch nicht implementierten Idee
(siehe Notizen zu „trickle_current logic — avoid unnecessary reduction at
SOC ≥ 98%"): Bisher reduzierte der Übergang von Block 3 (Vollladung fällig,
`max_a`/50A) in den Cellbalancing-Hold den Ladestrom unconditional auf
`trickle_current` (20A) — auch dann, wenn SOC und Spannung die
Vollladungs-Schwelle bereits erfüllt hatten und der höhere Strom technisch
unproblematisch weiterlaufen könnte. Das erzeugte einen unnötigen
Modbus-Write (50A → 20A) ohne Vorteil fürs Cellbalancing.

Fixed:
- `controller.py` (`decide`, Trickle/Hold-Block): Beim Eintritt in die
  Haltezeit wird jetzt `max(trickle_current, _ramp_current)` verwendet statt
  pauschal `trickle_current`. `_ramp_current` spiegelt den tatsächlich
  aktiven, gerampten Ladestrom des Vorzyklus wider (also z.B. 50A direkt aus
  Block 3) — der Strom wird nur dann auf `trickle_current` reduziert, wenn er
  vorher bereits niedriger war. Kommt der Hold aus Block 3, bleibt der Strom
  unverändert auf dem zuvor aktiven Wert (z.B. 50A), `_ramp()` erkennt
  `target_a == _ramp_current` und löst keinen weiteren Write aus.

Abgeglichen gegen den aktuellen Codepfad (Stand v3.0.13.1): bestätigt, dass
`self.state.charge_current_setpoint`/`self._ramp_current` zum Zeitpunkt des
`decide()`-Aufrufs noch den Wert des *vorherigen* Zyklus enthalten (Update
erfolgt erst danach in `run_cycle()`), und dass der Spezialfall
„`mode == 'trickle'` und Hold läuft → `decide()` wird jeden Zyklus neu
aufgerufen" (verhindert eingefrorenen Countdown) den ersten Übergang
full_charge→trickle nicht verzögert.

Nicht geändert: `_needs_full_charge()`, Trigger-/Abbruchbedingung des Holds
(v3.0.13.0/.1) bleiben unverändert — betroffen ist ausschließlich die Höhe
des beim Übergang gesetzten Stroms.

---

## v3.0.13.1 — Fix: Lücke zwischen Vollladung und Trickle ließ Strom auf 3A einbrechen (2026-06-28)

Symptom: Nach Einführung des Spannungskriteriums (v3.0.13.0) wurde beobachtet,
dass der Ladestrom während einer fälligen Vollladung abrupt von 50A auf 3A
sprang, statt durchgehend bei 50A zu bleiben bis Trickle einsetzt.

Log-Beleg (Pi-Journal, v3.0.13.0 produktiv, 28.06., `state.json`:
`days_since_full_charge: 1` zu Tagesbeginn → `dyn_target` korrekt 98.0 wegen
`full_charge_interval_days: 10` Defizit von Vortagen):

```
09:01:56  [FULL_CHARGE] 50A | Vollladung faellig (1 Tage) -> ... (aktuell 95.0%) [KEIN WRITE]
09:12:01  Modbus WRITE MaxChargeCurrent = 30 A
09:12:01  [IDLE] 30A | Morgen: PV im Optimal-Fenster ... warte
09:13:02  Modbus WRITE MaxChargeCurrent = 10 A
09:14:03  Modbus WRITE MaxChargeCurrent = 3 A
10:07:33  [IDLE] 3A | Ziel 98% erreicht (SOC 98.0%)
```

Dashboard zu diesem Zeitpunkt (10:30): SOC 98.0%, aber `battery_voltage`
**53.80 V** — deutlich unter der mit v3.0.13.0 geforderten Schwelle von
55.0V. Die Anlage zeigte "Ziel 98% erreicht" und pausierte mit 3A, obwohl
die Zellspannung das Vollladungs-Kriterium nie erreicht hatte.

Root Cause: Block 3 (`Vollladung fällig`) in `decide()` beendete sich bereits
bei `soc >= max_soc - hyst` (typ. 96%, mit `hyst=2`), während der
Trickle/Hold-Block weiter unten erst bei `soc >= dyn_target` (98%) und seit
v3.0.13.0 zusätzlich `battery_voltage >= full_charge_min_voltage` einsetzt.
Im Bereich 96–98% SOC — oder bei SOC 98% ohne bereits ausreichende
Zellspannung — griff **keiner** der beiden Blöcke. Die Steuerung fiel durch
in die Optimal-Fenster-/Idle-Logik weiter unten, die den Strom unabhängig
von der laufenden Vollladung auf einen niedrigen Wert (z.B.
`min_charge_current`, sichtbar als 3A) reduzierte. Diese Lücke existierte
bereits vor v3.0.13.0 (`max_soc - hyst` vs. `dyn_target` war schon vorher
keine identische Schwelle), wurde aber durch das zusätzliche
Spannungskriterium häufiger sichtbar, da SOC nun öfter kurz bei 98% liegt,
ohne dass die Spannungsbedingung schon erfüllt ist.

Fixed:
- `controller.py` (`decide`, Block 3 „Vollladung fällig“): Bedingung von
  `soc < max_soc - hyst` auf `not (soc >= 98.0 and battery_voltage >=
  full_charge_min_voltage)` geändert — identisch zur Freigabebedingung des
  Trickle-Blocks. Block 3 bleibt damit lückenlos bei `max_a` (50A) aktiv,
  bis exakt die Bedingung erfüllt ist, die den Trickle-Block übernimmt.
  Die Übergabe 50A → Trickle erfolgt dadurch ohne Zwischenstufe.

Nicht geändert: Trickle-/Hold-Logik (v3.0.13.0) und Auto-Reset-Timer
bleiben unverändert — betroffen war ausschließlich die Abbruchbedingung
von Block 3.

---

## v3.0.13.0 — Vollladungs-Kriterium verschärft: SOC + Spannung statt SOC allein (2026-06-28)

Symptom: Im Journal vom 27./28.06. erreichte SOC um 19:51 Uhr 98%, der
Cellbalancing-Hold startete (geplant 119 min bei `balancing_hold_hours`).
Um 20:51 griff der Auto-Reset (`SOC >= 98% für 61 Minuten`) und setzte
`days_since_full_charge` auf 0 — obwohl der Hold bereits um 21:11, nach nur
80 statt 119 Minuten, abbrach (SOC war zu diesem Zeitpunkt bereits wieder
auf 97% gefallen). Die als abgeschlossen verbuchte Vollladung war also nie
stabil erreicht. Folge: in der Nacht zum 28.06. (03:25 Uhr, kein PV) löste
`_needs_full_charge()` erneut aus, 50A wurden geschrieben, SOC blieb aber
über Stunden bei 84–87% hängen statt zu steigen — ein zweiter erfolgloser
Versuch, weil ohne PV-Überschuss kein nennenswerter Ladestrom fließt.

Root Cause: Der Auto-Reset und der Cellbalancing-Hold basierten ausschließlich
auf `SOC >= 98%`. Bei LiFePO4 ist die Spannungskurve um 98% SOC sehr flach —
ein kurzer SOC-Peak (z.B. durch Lastschwankung oder Mess-Ungenauigkeit der
SOC-Schätzung) erreicht leicht 98%, ohne dass die Zellen tatsächlich auf
Vollladespannung sind. SOC allein ist damit kein verlässliches Kriterium für
"Batterie ist wirklich voll" — und genau das braucht eine Zellbalancing-Phase.

Fixed:
- `controller.py` (`run_cycle`, Auto-Reset-Block): Trigger für den 60-Minuten-
  Reset-Timer (`_soc_98_reached_at`) erfordert jetzt `soc >= 98.0` UND
  `battery_voltage >= full_charge_min_voltage` (neuer Konfig-Parameter,
  Default 55.0 V) gleichzeitig. Fällt eine der beiden Bedingungen wieder
  unter die Schwelle, bricht der Timer sofort ab (kein Hysterese-
  Gnadenintervall) — ein instabiler Peak darf den Reset nicht auslösen.
- `controller.py` (`decide`, Cellbalancing-Hold): Während der laufenden
  Haltezeit (`balancing_hold_hours`) muss `soc >= 98.0` UND
  `battery_voltage >= full_charge_min_voltage - full_charge_voltage_hysteresis`
  (neuer Konfig-Parameter, Default 0.1 V) erfüllt bleiben. Unterschreitet
  SOC oder Spannung die Schwelle, bricht der Hold sofort ab
  (`_balancing_hold_until = 0`, `_soc_98_reached_at = None`) und
  `days_since_full_charge` bleibt unverändert — die Vollladung gilt als
  nicht erfolgreich und wird beim nächsten PV-Überschuss erneut versucht,
  statt nachts wirkungslos mit 50A gegen fehlenden Überschuss anzulaufen.
- Neue Konfig-Parameter in `config.yaml` unter `battery:`:
  - `full_charge_min_voltage` (Default 55.0): Spannungsschwelle für den
    Trigger, passend zur 16S-LiFePO4-Konfiguration (Vollladung ca. 54,4–56V
    je nach Zellchemie/Toleranz).
  - `full_charge_voltage_hysteresis` (Default 0.1): Hysterese-Abstand für
    das Halten der Trickle-Phase (Trigger-Schwelle minus Hysterese =
    Abbruch-Schwelle, z.B. 55.0V Trigger → 54.9V Abbruch).

Nicht geändert: `_needs_full_charge()` selbst (reine Tageszähler-Logik) und
der Vollladungs-Pfad (`dyn_target >= 98.0`, max_a-Laden) bleiben unverändert —
betroffen ist ausschließlich die Frage, *wann eine begonnene Vollladung als
abgeschlossen gilt*.

Risiko/Rollback: Bei fehlender oder nicht kalibrierter `battery_voltage`-
Messung (z.B. Defaultwert 0 oder unplausibler Wert) würde der Auto-Reset nie
mehr greifen und `days_since_full_charge` ständig hochzählen → tägliche
Vollladeversuche. Vor dem Deploy battery_voltage-Plausibilität im aktuellen
state.json/Log prüfen (im vorliegenden Journal werden Werte nicht geloggt,
aber `actual_v` wird in `decide()` bereits seit v3.0.x aus `state.battery_voltage`
mit Fallback auf `nom_v * 0.875` gebildet — die Messung existiert also bereits
und wird hier erstmals für eine Entscheidung statt nur für `_simulate_hour()`
verwendet).

---

## v3.0.12.2 — Fix: Unnötige Rampe statt Direktsprung nach Sonnenuntergang (2026-06-20)

Korrektur zu v3.0.12.1: die dortige Notiz ging davon aus, dass `controller.py`
(`_ramp`) die Nacht-Ausnahme bereits enthält. Beim Abgleich mit der tatsächlich
auf dem Pi laufenden Datei (Quelle des analysierten Journals) zeigte sich:
die Ausnahme fehlte dort komplett — `_ramp()` rampte unabhängig von Tag/Nacht
immer in `current_ramp_step`-Schritten. Log-Beleg (2026-06-19, nach
Sonnenuntergang 21:26):

```
22:07:58  Modbus WRITE MaxChargeCurrent = 40 A   [IDLE] Nacht: kein Laden
22:08:59  Modbus WRITE MaxChargeCurrent = 30 A
22:09:59  Modbus WRITE MaxChargeCurrent = 20 A
22:11:00  Modbus WRITE MaxChargeCurrent = 10 A
22:12:01  Modbus WRITE MaxChargeCurrent = 3 A
```

5 Writes über 4 Minuten, obwohl PV zu diesem Zeitpunkt bereits bei 0 liegt
und der reale Ladestrom ohnehin durch DVCC/ESS auf das begrenzt wird, was
PV liefert — ein Direktsprung 50A→3A hätte am tatsächlich fließenden Strom
nichts geändert.

Fixed:
- `controller.py` (`_ramp`): Nacht-Ausnahme ergänzt. Vor Sonnenaufgang bzw.
  nach Sonnenuntergang (`h_now < sunrise or h_now > sunset`, astronomisch
  über `forecast._calculate_sun_times()`) wird `target_a` direkt gesetzt
  (`_ramp_current = target_a`) statt schrittweise anzunähern. Reduziert
  Modbus-Writes beim Übergang in/aus FULL_CHARGE in der Dunkelphase von
  bis zu 5 auf 1, ohne die Rampen-Dämpfung tagsüber (PV-Schwankungen) zu
  beeinflussen — die Bedingung greift ausschließlich außerhalb des
  Sonnenauf-/untergangsfensters.

  Simulation mit den geloggten Zeitstempeln und den realen Standort-Koordinaten
  aus `config.yaml` bestätigt: erster Zyklus nach Sonnenuntergang springt
  jetzt direkt von 50A auf 3A (Clamp durch `min_charge_current`), alle
  Folgezyklen schreiben nicht erneut (Hysterese).

- `version.py`: VERSION auf 3.0.12.2 aktualisiert.

---

## v3.0.12.1 — Klarstellung: Rampe an Sonnenauf-/untergang (2026-06-20)

Notiz (keine Code-Änderung):
- Frage geprüft: ob beim Übergang in/aus der Dunkelphase ein zusätzlicher
  Zeitpuffer vor Sonnenaufgang bzw. nach Sonnenuntergang sinnvoll wäre, um
  unnötige Modbus-Writes beim Herunter-/Hochrampen (z.B. 50A→3A) zu vermeiden.
  Begründung des Vorschlags: die PV-Leistung ist in diesen Randstunden so
  gering, dass ohnehin nur wenige A Ladestrom fließen können — selbst bei
  einem direkten Sprung 3A→50A oder 50A→3A ändert sich am tatsächlich
  fließenden Strom nichts, da DVCC/ESS den Strom automatisch auf das
  begrenzen, was PV liefert.
- Ergebnis: kein zusätzlicher Puffer nötig. `controller.py` (`_ramp`)
  enthält bereits genau dieses Verhalten (seit der Modularisierung,
  v3.0.10.x): `is_night = h_now < sunrise or h_now > sunset` springt bei
  Nacht direkt auf den Zielwert, ganz ohne Zwischenschritte — kein
  zusätzlicher Zeitpuffer vor/nach der reinen Sonnenauf-/untergangsgrenze.
  Sichtbare Rampen-Sequenzen außerhalb der Nachtphase (z.B. gegen 16 Uhr,
  deutlich vor Sonnenuntergang) sind reguläre Tageslicht-Übergänge
  (FULL_CHARGE → Trickle), keine Dämmerungsfälle, und dort ist das Rampen
  weiterhin korrekt und gewollt (PV schwankt zu dieser Zeit noch spürbar).
- `version.py`: VERSION auf 3.0.12.1 aktualisiert.

---

## v3.0.12 — Neues Feature: Winterpause (2026-06-18)

Added:
- `controller.py` (`_in_winter_pause`): Prüft, ob das heutige Datum im
  konfigurierten Winterpause-Zeitraum liegt. `winter_pause_start`/`winter_pause_end`
  sind MM-DD-Strings (jahresunabhängig), ein Zeitraum über den Jahreswechsel
  hinweg (z.B. `11-01` → `02-28`) wird korrekt überbrückt.
- `controller.py` (`decide`): Neuer Prioritäts-Block **0** — höchste Priorität,
  läuft explizit vor ESS State 11/12 und allen anderen Entscheidungspfaden.
  Solange die Winterpause aktiv ist, übernimmt `decide()` keine Regelung mehr:
  beim Eintritt in den Zeitraum wird einmalig `max_charge_current` per Modbus
  geschrieben (`victron.set_max_charge_current`), danach liefert `decide()`
  durchgehend `(-1, "winter_pause", ...)` zurück — `run_cycle()` überspringt
  damit den Write-Block komplett (analog zur evcc-Schnelllade-Priorität).
  Verlässt das Datum den Zeitraum, wird das interne Write-Flag
  (`_winter_pause_write_done`) zurückgesetzt, damit im nächsten Winter wieder
  einmalig geschrieben wird.
- `config.yaml`: neue Keys `winter_pause_enabled` (Default `false`),
  `winter_pause_start` (Default `"11-01"`), `winter_pause_end`
  (Default `"02-28"`).

  Sandbox-Testlauf (Konsole + eigenständiges Test-Dashboard auf Port 5001,
  parallel zum echten Service auf Port 5000) bestätigt: genau 1 Modbus-Write
  beim Eintritt, 0 weitere Writes über mehrere Zyklen, `state.charge_mode`/
  `state.charge_reason` zeigen `winter_pause` korrekt im Dashboard an.

Fixed:
- `controller.py` (`decide`): `NameError: name 'needs_full' is not defined`
  beim Eintritt ins Optimal-Fenster (Block 6). Die in v3.0.11.4 eingefuehrte
  Bedingung `if not needs_full:` referenzierte eine lokale Variable, die in
  `decide()` nie zugewiesen wird — `needs_full` existiert nur als lokale
  Variable innerhalb von `build_schedule()` (dort via
  `needs_full = self._needs_full_charge()`). Der Crash betraf ausschliesslich
  Zyklen innerhalb des Optimal-Fensters (rund um Sonnenhoechststand) und
  fuehrte zu einer Restart-Schleife des systemd-Service (`status=1/FAILURE`,
  `Scheduled restart job`), sobald `decide()` diesen Codepfad erreichte.

  Fix: `if not needs_full:` → `if not self._needs_full_charge():`, also
  Aufruf der bereits vorhandenen Helper-Methode statt Referenz auf eine
  nicht existierende lokale Variable.

  Bestaetigt im Live-Betrieb (2026-06-19, H11): Optimal-Fenster-Plan laeuft
  fehlerfrei durch, Modbus-Write erfolgreich (13 A), Dashboard zeigt
  `Optimal-Fenster H11: 13A (Plan 702Wh, Übertrag +0Wh, SOC 47.0%→71%)`.

- `version.py`: VERSION auf 3.0.12 aktualisiert.

---

## v3.0.11.5 — Bugfix: Phantomstrom ~148 A bei H00 (Mitternachts-Reset-Reihenfolge) (2026-06-17)

Fixed:
- `controller.py` (`run_cycle`): Mitternachts-Reset wurde **nach** `_update_history()`
  ausgefuehrt. `_update_history()` legt um 00:00 den ersten H00-Eintrag an und
  speichert dabei `_hour_start_bat_wh = bat_wh_total` — zu diesem Zeitpunkt noch
  mit der alten kumulierten Tagesbasis (z.B. -7844 Wh bei SOC 80%). Erst danach
  setzte der Reset `_energy_base_bat = 0.0` und der `EnergyAccumulator` startete
  neu ab 0. Alle Folge-Updates in H00 berechneten dann:

  ```
  bat_wh_hour = bat_wh_total_neu - _hour_start_bat_wh_alt
              = -4 Wh - (-7844 Wh) = +7840 Wh -> +148 A
  ```

  Fix: Mitternachts-Reset vor `_update_history()` verschieben. Beim Anlegen des
  H00-Eintrags ist `bat_wh_total = 0 + 0 = 0` korrekt, alle Folge-Updates
  rechnen ab dem richtigen Ursprung.

  Beobachtetes Symptom (Screenshot v3.0.11.4, 04:09):
  ```
  00:00  PAUSE  -148.0 A  80.0%   <- war +7840 Wh / 53V = 147.9 A
  01:00  PAUSE    -5.3 A  78.0%   <- korrekt
  ```

- `version.py`: VERSION auf 3.0.11.5 aktualisiert.

---

## v3.0.11.4 — Bugfix: Unnötige Stromrampe beim Übergang FULL_CHARGE→TRICKLE (2026-06-16)

Fixed:
- `controller.py` (`run_cycle`): Beim Übergang von VOLLLADUNG (50A) zu TRICKLE
  (20A) wurde das Optimal-Fenster fälschlicherweise noch aktiv, obwohl
  `needs_full=True` und `soc >= max_soc - hyst` (≥97%). Das führte zur
  Modbus-Sequenz 50A→40A→30A→20A→10A (Optimal-Fenster-Plan) gefolgt von
  sofortigem 10A→20A (Trickle-Rampe hoch).

  Fix: Am Eingang des Optimal-Fenster-Blocks prüfen ob `needs_full`.
  Bei aktiver Vollladung überspringt der Block komplett (`if not needs_full:`),
  sodass `decide()` den Volllade-Strom (max_a) und den Trickle-Pfad direkt
  steuert. Die ursprüngliche Bedingung `soc >= max_soc - hyst` war zu eng
  — das Optimal-Fenster hätte schon bei z.B. 90% SOC störend auf 15A
  reduziert, bevor dann Trickle wieder auf 20A hochrampt.

- `controller.py` (`_simulate_hour`): Simulation modellierte den Trickle-/
  Balancing-Haltezeit-Pfad nicht. Bei `needs_full and soc_sim >= max_soc - hyst`
  wurde `_apply_deficit()` aufgerufen (3A `min_charge_current`), statt
  `trickle_current` (20A). Fix: Neuer Pfad vor dem bestehenden `needs_full`-Block:
  wenn `soc_sim >= max_soc - hyst` → `trickle_current` für diese Stunde simulieren,
  SOC geclampt auf `[floor_soc, max_soc]`. Vereinfachung gegenüber realem
  `_balancing_hold_until` (Laufzeit-State), aber korrekt für Anzeigezwecke.

- `version.py`: VERSION auf 3.0.11.4 aktualisiert.

---

## v3.0.11.3 — Bugfix: Doppel-Heartbeat im Journal (2026-06-16)

Fixed:
- `logging_setup.py` (`DeduplicatingFilter.emit_heartbeat_if_due`): Bei
  nicht-HTTP-Nachrichten (z.B. `[FULL_CHARGE] ... [KEIN WRITE]`) wurden
  Heartbeats doppelt geschrieben — einmal durch `filter()` (appended
  `(Heartbeat)` an `record.msg`) und gleichzeitig durch den
  Hintergrund-Thread via `emit_heartbeat_if_due()` (schreibt
  `- (Heartbeat: kein Browser-Request seit 20min)`). Ursache: Race
  Condition — beide Pfade pruefen `_last_ts` fast gleichzeitig und
  sehen es als faellig.

  Fix: `emit_heartbeat_if_due()` feuert nur noch fuer `HTTP_ACCESS`-
  Nachrichten. Fuer alle anderen Nachrichten ist `filter()` allein
  zustaendig. Das `else`-Branch (`text = last_msg + " (Heartbeat)"`)
  in `emit_heartbeat_if_due()` ist damit entfallen.

  Beobachtetes Symptom im Journal (vor Fix):
  ```
  [FULL_CHARGE] 50A | ... [KEIN WRITE] (Heartbeat)
  - (Heartbeat: kein Browser-Request seit 20min)
  ```

- `version.py`: VERSION auf 3.0.11.3 aktualisiert.

---

## v3.0.11.2 — Bugfix: Simulations-SOC ueber 100% (2026-06-16)

Fixed:
- `controller.py` (`_simulate_hour`): Der projizierte SOC im Ladeplan konnte
  im PAUSE-Zustand (nach Erreichen von `dyn_target`) ueber 100% bzw. ueber
  `max_soc` (98%) ansteigen. Ursache: `_apply_deficit()` kappte `new_soc`
  zwar nach unten auf `floor_soc`, aber nicht nach oben. Bei positivem
  PV-Ueberschuss (`deficit = 0`) addierte der Trickle-Strom (`min_charge_a`)
  den SOC jede Stunde leicht — unkontrolliert bis >105%.

  Fix an zwei Stellen:
  1. `_apply_deficit()`: `new_soc = min(max_soc, new_soc)` nach dem
     `floor_soc`-Clamp.
  2. `soc_sim >= dyn_target`-Block: `soc_sim = min(soc_sim, max_soc)` nach
     dem `floor_soc`-Clamp, vor dem `return`.

- `version.py`: VERSION auf 3.0.11.2 aktualisiert.

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
