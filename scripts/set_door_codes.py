"""Türcodes einmalig übernehmen: pro Termin (Datum + Code von stdin) und/oder
als Saison-Code des Abos. Danach pflegt man sie im Admin weiter (Abo-Formular
bzw. Terminseite).

Eine Zeile pro Termin, Datum als JJJJ-MM-TT, dann der Code:
    2026-10-15 599887
    2026-10-22 658439

Im Produktions-Container (als User sportabo, damit .env und DB passen):
    cd /opt/sportabo && runuser -u sportabo -- venv/bin/python \\
        scripts/set_door_codes.py "Abo-Name" [--season CODE] < codes.txt

Die Codes gehören nicht ins Repo (öffentlich) — nur per stdin übergeben.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal  # noqa: E402
from app.models.models import Event, Subscription  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("abo", help="Name des Abos (exakt)")
parser.add_argument("--season", help="Türcode für die ganze Saison")
args = parser.parse_args()

codes: dict[date, str] = {}
if not sys.stdin.isatty():
    for n, line in enumerate(sys.stdin, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            day, code = line.split(maxsplit=1)
            codes[date.fromisoformat(day)] = code.strip()
        except ValueError:
            sys.exit(f"Zeile {n} unlesbar: {line!r} (erwartet: JJJJ-MM-TT CODE)")

db = SessionLocal()
try:
    sub = db.query(Subscription).filter(Subscription.name == args.abo).first()
    if sub is None:
        names = ", ".join(s.name for s in db.query(Subscription).all())
        sys.exit(f"Abo {args.abo!r} nicht gefunden. Vorhanden: {names}")
    if args.season is not None:
        sub.door_code = args.season.strip()
        print(f"Saison-Code für {sub.name!r} gesetzt.")
    events = {
        e.date: e
        for e in db.query(Event).filter(
            Event.subscription_id == sub.id, Event.is_extra == False  # noqa: E712
        )
    }
    missing = []
    for day, code in sorted(codes.items()):
        if day in events:
            events[day].door_code = code
        else:
            missing.append(day)
    db.commit()
    print(f"{len(codes) - len(missing)} Termin(e) mit eigenem Türcode gesetzt.")
    for day in missing:
        print(f"  kein Termin am {day.isoformat()} — übersprungen")
finally:
    db.close()
