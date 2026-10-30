"""Versehentliche „Bezahlt ✓“-Klicks bei einem Link-Gast aus dem Kontoauszug
entfernen.

Jedes „Bezahlt ✓“ im Vorstreck-Modell bucht beim Zahlungsempfänger
„Gastzahlung von <Gast> erhalten“ (−X), jedes „Zurücknehmen“ dazu
„Storno Gastzahlung von <Gast>“ (+X). Das Skript sucht je Termin und Betrag
Storno-Buchungen samt der jeweils davor liegenden Erhalten-Buchung und
löscht diese Paare. Jedes Paar ergibt 0 — Guthaben und Bezahlt-Status
bleiben gleich, übrig bleibt die letzte (gültige) Erhalten-Buchung.

Ohne --apply wird nur angezeigt, was gelöscht würde.

Im Produktions-Container (als User sportabo, damit .env und DB passen):
    cd /opt/sportabo && runuser -u sportabo -- venv/bin/python scripts/remove_guest_paid_reversals.py Bernhard
    cd /opt/sportabo && runuser -u sportabo -- venv/bin/python scripts/remove_guest_paid_reversals.py Bernhard --apply
"""

import argparse
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal  # noqa: E402
from app.models.models import Payment  # noqa: E402


def find_reversal_pairs(db, guest_name: str) -> tuple[list, list]:
    """(Paare [(erhalten, storno)], übrige Buchungen) für den Gast."""
    received_note = f"Gastzahlung von {guest_name} erhalten"
    storno_note = f"Storno Gastzahlung von {guest_name}"
    rows = (
        db.query(Payment)
        .filter(Payment.note.in_([received_note, storno_note]))
        .order_by(Payment.created_at, Payment.id)
        .all()
    )
    groups = defaultdict(list)
    for p in rows:
        groups[(p.member_id, p.event_id, abs(p.amount))].append(p)
    pairs, rest = [], []
    for entries in groups.values():
        open_received = []
        for p in entries:
            if p.note == received_note:
                open_received.append(p)
            elif open_received:
                pairs.append((open_received.pop(), p))
            else:
                rest.append(p)  # Storno ohne vorherige Erhalten-Buchung
        rest.extend(open_received)
    return pairs, rest


def _line(p: Payment) -> str:
    event = p.event.date.strftime("%d.%m.%Y") if p.event else "—"
    return (
        f"  {p.created_at:%d.%m.%Y %H:%M}  {p.member.name:<15} Termin {event}  "
        f"{p.amount:>8}  {p.note}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("guest", help="Name des Gasts, genau wie in der Buchung")
    parser.add_argument("--apply", action="store_true", help="wirklich löschen")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        pairs, rest = find_reversal_pairs(db, args.guest)
        print(f"Bleiben stehen ({len(rest)}):")
        for p in rest:
            print(_line(p))
        print(f"Zu löschen ({len(pairs)} Paare):")
        for received, storno in pairs:
            print(_line(received))
            print(_line(storno))
        if not pairs:
            print("  nichts zu tun")
            return 0
        total = sum((r.amount + s.amount for r, s in pairs), Decimal("0"))
        assert total == 0, f"Paare ergeben nicht 0 ({total}) — Abbruch"
        if not args.apply:
            print("\nNur Anzeige. Mit --apply wirklich löschen (Guthaben ändern sich nicht).")
            return 0
        for received, storno in pairs:
            db.delete(received)
            db.delete(storno)
        db.commit()
        print(f"\n{len(pairs) * 2} Buchungen gelöscht, Guthaben unverändert.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
