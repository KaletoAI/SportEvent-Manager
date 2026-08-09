# Usermanual mit Mobile-Screenshots — Design

**Datum:** 2026-08-03
**Status:** freigegeben

## Ziel

Ein Handbuch für Nutzer des SportAbo Managers, das erklärt, wie man ein Abo
beantragt, die App als Mitglied bzw. Super-Mitglied bedient, als Gast bucht
und welche E-Mails das System verschickt. Es muss sich als PDF exportieren
lassen und Mobile-Screenshots enthalten.

## Artefakt

`docs/usermanual.html` — eine einzelne, self-contained HTML-Datei:

- Screenshots als `data:image/png;base64,…` eingebettet, damit die Datei
  ohne Bilderordner weitergegeben werden kann.
- Print-CSS für A4: der Nutzer öffnet die Datei im Browser und wählt
  „Drucken → Als PDF sichern".
- Kein CSS-Framework, deutsche Sprache (du-Form), Duktus wie die App.

## Kapitel

1. **Kurzüberblick** — Kostenteilungsmodell in wenigen Sätzen:
   Abo-Gesamtpreis wird auf die offenen Termine verteilt, das Termin-Budget
   auf die tatsächlichen Teilnehmer. Mitglieder zahlen den Anteil am
   Abo-Preis, Gäste den Anteil am Normalpreis. Ein Termin findet erst ab
   der Mindestteilnehmerzahl statt.
2. **Abo anlegen** — Screenshot des ausgefüllten Abo-Formulars plus
   Feldtabelle mit Bedeutung und der Angabe, was man dem Admin melden muss:
   Name, Beschreibung, Wochentag, Beginn, Dauer, Min./Max.-Teilnehmer,
   Zeitraum (Start/Ende), Abo-Gesamtpreis, Normalpreis gesamt,
   `cancel_hours_free`, `cancel_hours_approval`, Auszahlungsmodus,
   PayPal-Adresse. Dazu: Termine generieren, Mitglieder anlegen
   (inkl. Super-Flag und Personen-Verzeichnis).
3. **Als Mitglied** — Login per Magic Link bzw. 6-stelligem Code,
   Dashboard-Tabs (📅 Termine, ✅ Meine, 💶 Konto), Anmelden mit Gästen,
   Preisanzeige „max. X €", Abmeldefristen (frei / Freigabe nötig / gar
   nicht), Warteliste, Teilnehmerliste, Kontoauszug, Guthaben,
   „Zahlungseingang bestätigen", Minus-Banner mit Zahlungsempfänger,
   Abo-Umschalter.
4. **Super-Mitglied** — der zusätzliche Tab ⚙️ Verwaltung: Storno-Anfragen
   freigeben/ablehnen, Termine abrechnen, Termin stornieren (Preis
   reduzieren vs. umlegen), Zusatztermin anlegen, Gastzahlungen als bezahlt
   markieren, volle Preisstaffel statt nur Maximalpreis.
5. **Als Gast** — öffentlicher Link `/g/{token}`, Buchungsformular
   (Name, E-Mail, Anzahl), Bestätigungsseite mit Preis und PayPal-Empfänger,
   kein Online-Abmelden (Organisator informieren).
6. **E-Mails** — Tabelle aller sieben Mails mit Auslöser, Empfänger, Inhalt
   und Handlungsbedarf:
   - Anmelde-Link (Login angefordert → Mitglied)
   - Erinnerung Abmeldefrist (1 Tag vor letzter freier Abmeldung → Mitglied)
   - Termin-Erinnerung Gast (gleicher Zeitpunkt → Gast mit E-Mail)
   - Nachgerückt von der Warteliste (Platz frei → Mitglied)
   - Storno-Anfrage (Abmeldung nach Frist → Super-Mitglieder)
   - Abrechnung (Termin abgerechnet → angemeldete Mitglieder)
   - Abrechnung Gast (Termin abgerechnet → Gäste mit E-Mail, unbezahlt)

## Screenshots

24 Bilder in der Breite 390 CSS-Pixel bei `device_scale_factor=2`, erzeugt
mit Playwright (Chromium headless). Der Viewport wächst pro Aufnahme auf die
Seitenhöhe, statt `full_page` zu benutzen — sonst rendert Chromium die
`position: fixed` Kopf- und Tableiste mehrfach mitten ins Bild. Seiten, die
dabei länger als etwa 1200 CSS-Pixel würden, werden in zwei Bilder geteilt
oder auf den aussagekräftigen Ausschnitt beschnitten, damit die Screenshots
im PDF lesbar bleiben.

Damit die Emoji der Tab-Leiste nicht als leere Kästchen erscheinen, muss
`fonts-noto-color-emoji` installiert sein.

## Skripte (werden committet)

- `scripts/demo_seed.py` — legt eine separate SQLite-Datei mit
  Demo-Daten an: Abo „Beachvolleyball Dienstag", acht Mitglieder mit
  Fantasienamen (davon ein Super-Mitglied), vergangene abgerechnete und
  kommende Termine, Mitglieder- und Gastbuchungen, ein ausgebuchter Termin
  mit Warteliste, eine offene Storno-Anfrage, Guthaben im Plus und im Minus.
  Termine werden relativ zum echten heutigen Datum erzeugt, damit kein
  Test-Datum-Override nötig ist.
- `scripts/make_screenshots.py` — startet eine Uvicorn-Instanz auf einem
  freien Port gegen die Demo-DB (Scheduler aus, SMTP leer, damit keine
  echten Mails rausgehen), legt Sessions für Admin, Mitglied und
  Super-Mitglied direkt in der DB an, navigiert die Seiten an und schreibt
  die PNGs nach `docs/screenshots/`.
- `scripts/build_manual.py` — baut jede `docs/*.src.html` zur ausliefernden
  Datei `docs/*.html`, indem es die Screenshots als `data:`-URI einbettet und
  dabei auf eine 256-Farben-Palette reduziert (rund ein Drittel der
  Ausgangsgröße, optisch unverändert).

## Kurzreferenz (Nachtrag)

Neben dem Handbuch gibt es `docs/kurzreferenz.html` — ein Handout für
Mitglieder und Gäste ohne das Admin-Kapitel, ausgelegt auf **genau drei
A4-Seiten**. Es nutzt dieselben Screenshots (vier davon, klein gesetzt) und
ein eigenes, kompakteres Print-CSS. Wer daran etwas ergänzt, muss die
Seitenzahl nachprüfen: als PDF drucken und zählen.

Ablauf bei UI-Änderungen: `demo_seed.py` → `make_screenshots.py` →
`build_manual.py`.

Beide Skripte fassen weder die Dev- noch die Prod-Datenbank an: die Demo-DB
liegt unter einem eigenen Pfad und wird per `DATABASE_URL` übergeben.

## Auslieferung über /hilfe (Nachtrag)

Beide Dokumente sind auf der Website erreichbar. `scripts/build_manual.py`
schreibt sie nach `app/help/`, weil das Docker-Image nur `app/` kopiert.
Bewusst nicht nach `app/static/`: das ist öffentlich gemountet und würde
die Anmeldeprüfung des Handbuchs aushebeln.

`app/routes/help.py` liefert aus:

| Pfad | Inhalt | Zugriff |
| --- | --- | --- |
| `/hilfe` | Übersichtsseite im App-Design | öffentlich |
| `/hilfe/kurzreferenz` | Kurzreferenz | öffentlich |
| `/hilfe/handbuch` | Handbuch | nur mit gültiger Session (Mitglied oder Admin) |

Die Kurzreferenz bleibt öffentlich, weil Gäste kein Konto haben und über
den Buchungslink kommen — die Gastseite verlinkt sie. Das Handbuch ist
geschützt, weil es das Admin-Kapitel mit Formularen, Admin-Pfaden und der
Preislogik enthält.

Ein ❓-Link in der Navigation (base.html) führt von jeder Seite zur
Übersicht.

## Nicht-Ziele

- Keine Änderung an der Fachlogik der App.
- Keine englische Fassung.
