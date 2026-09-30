#!/usr/bin/env python3
"""Demo-Datenbank für die Screenshots im Usermanual befüllen.

Erzeugt eine eigenständige SQLite-Datei mit erfundenen Personen und einem
kompletten Abo-Lebenszyklus (abgerechnete und kommende Termine, Gäste,
Warteliste, offene Storno-Anfrage, Guthaben im Plus und im Minus). Die
Daten sind rein fiktiv — echte Dev- und Prod-Datenbanken bleiben unberührt.

    DATABASE_URL=sqlite:////tmp/demo.db python scripts/demo_seed.py

Ohne DATABASE_URL wird ./data/demo.db verwendet. Eine bereits vorhandene
Datei wird gelöscht, damit jeder Lauf identische Screenshots ergibt.
"""

import os
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Muss vor dem App-Import stehen: database.py liest die URL beim Import.
os.environ.setdefault("DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'demo.db'}")
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["SMTP_HOST"] = ""  # keine echten Mails aus dem Seed heraus

DB_PATH = os.environ["DATABASE_URL"].split("sqlite:///", 1)[-1]
if Path(DB_PATH).exists():
    Path(DB_PATH).unlink()

from app.database import SessionLocal, engine  # noqa: E402
from app.models.models import (  # noqa: E402
    Base,
    Booking,
    Event,
    GuestBooking,
    Member,
    Payment,
    Subscription,
    WaitlistEntry,
    utcnow,
)
from app import services  # noqa: E402

# ── Demo-Personen (frei erfunden) ─────────────────────────────────────────
# (Name, E-Mail, Super?, PayPal)
PEOPLE = [
    ("Lena Sommer", "lena.sommer@example.com", True, "lena.sommer@example.com"),
    ("Jonas Berg", "jonas.berg@example.com", False, ""),
    ("Mira Falk", "mira.falk@example.com", False, ""),
    ("Tobias Reich", "tobias.reich@example.com", False, ""),
    ("Anna Wolter", "anna.wolter@example.com", False, ""),
    ("Sven Kramer", "sven.kramer@example.com", False, ""),
    ("Nadja Hoff", "nadja.hoff@example.com", False, ""),
    ("Paul Brenner", "paul.brenner@example.com", False, ""),
]

WEEKDAY = 1  # Dienstag
START = time(18, 30)
DURATION = 120


def previous_weekday(ref: date, weekday: int) -> date:
    """Letzter Termin-Wochentag, der echt vor `ref` liegt."""
    delta = (ref.weekday() - weekday) % 7 or 7
    return ref - timedelta(days=delta)


def next_weekday(ref: date, weekday: int) -> date:
    """Nächster Termin-Wochentag ab `ref` (heute zählt nicht)."""
    delta = (weekday - ref.weekday()) % 7 or 7
    return ref + timedelta(days=delta)


def main() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    today = date.today()

    # Der Abo-Zeitraum umschließt 4 vergangene und 5 kommende Dienstage —
    # genug für alle Zustände, ohne dass die Listen im Screenshot ausufern.
    first = previous_weekday(today, WEEKDAY) - timedelta(days=7 * 3)
    last = next_weekday(today, WEEKDAY) + timedelta(days=7 * 4)

    sub = Subscription(
        name="Beachvolleyball Dienstag",
        description=(
            "Feste Platzmiete dienstags auf Platz 3, Sommersaison. "
            "Der Platz ist für die ganze Saison gebucht und bezahlt — "
            "wir teilen die Kosten pro Termin durch alle, die da waren."
        ),
        weekday=WEEKDAY,
        start_time=START,
        duration_minutes=DURATION,
        start_date=first,
        end_date=last,
        abo_price=Decimal("480.00"),
        default_price=Decimal("624.00"),
        min_participants=4,
        max_participants=8,
        cancel_hours_free=48,
        cancel_hours_approval=12,
        payout_mode="member",
        paypal_address="kasse.beachclub@example.com",
    )
    db.add(sub)
    db.flush()

    members = []
    for name, email, is_super, paypal in PEOPLE:
        m = Member(
            subscription_id=sub.id,
            name=name,
            email=email,
            credit=Decimal("0.00"),
            is_super=is_super,
            paypal_address=paypal,
        )
        db.add(m)
        members.append(m)
        services.upsert_person(db, name, email, paypal)
    db.flush()

    # ── Termine für den ganzen Zeitraum erzeugen ─────────────────────────
    current = first
    events = []
    while current <= last:
        if current.weekday() == WEEKDAY:
            end_dt = datetime.combine(current, START) + timedelta(minutes=DURATION)
            ev = Event(
                subscription_id=sub.id,
                date=current,
                start_time=START,
                end_time=end_dt.time(),
                max_participants=sub.max_participants,
                min_participants=sub.min_participants,
            )
            db.add(ev)
            events.append(ev)
        current += timedelta(days=1)
    db.flush()
    db.refresh(sub)
    services.recompute_budgets(db, sub)
    db.commit()

    past = [e for e in events if e.date < today]
    future = [e for e in events if e.date >= today]

    # ── Einzahlungen ────────────────────────────────────────────────────
    # Alle zahlen zum Saisonstart ein — außer Paul Brenner, der dadurch
    # nach seiner ersten Teilnahme im Minus steht und im Handbuch den
    # Zahlungs-Banner zeigt.
    for m in members:
        if m.name == "Paul Brenner":
            continue
        amount = Decimal("60.00")
        db.add(
            Payment(
                member_id=m.id,
                amount=amount,
                type=Payment.TYPE_DEPOSIT,
                note="Einzahlung Saisonstart",
            )
        )
        m.credit += amount
    db.commit()

    # ── Vergangene Termine: buchen und abrechnen ─────────────────────────
    # Wechselnde Besetzungen, damit der Kontoauszug abwechslungsreich ist.
    rosters = [
        ([0, 1, 2, 3, 4, 5], {1: 1}),
        ([0, 2, 3, 6, 7], {}),
        ([0, 1, 4, 5, 6], {0: 1}),
        ([0, 1, 2, 3, 7], {}),
    ]
    guest_names = [
        ("Kim Roth", "kim.roth@example.com", 1),
        ("Ellen Marx", "ellen.marx@example.com", 2),
    ]
    # Der jüngste vergangene Termin bleibt bewusst offen, damit im
    # Verwaltungs-Tab der Super-Mitglieder ein abzurechnender Termin steht.
    for idx, ev in enumerate(past):
        idxs, guests = rosters[idx % len(rosters)]
        for i in idxs:
            db.add(
                Booking(
                    event_id=ev.id,
                    member_id=members[i].id,
                    guest_count=guests.get(i, 0),
                )
            )
        # Auf zwei vergangenen Terminen war je ein Link-Gast dabei.
        if idx < len(guest_names):
            gname, gmail, gcount = guest_names[idx]
            gb = GuestBooking(
                event_id=ev.id, name=gname, email=gmail, count=gcount
            )
            db.add(gb)
        db.commit()
        if ev is not past[-1]:
            services.settle_event(db, ev)

    # Der Gast des ersten Termins hat bezahlt, der zweite noch nicht.
    first_guest = (
        db.query(GuestBooking)
        .filter(GuestBooking.event_id == past[0].id)
        .first()
    )
    if first_guest:
        shares = services.event_shares(db, past[0])
        services.mark_guest_paid(
            db, first_guest, shares["guest_share"] * first_guest.count
        )

    # ── Kommende Termine ────────────────────────────────────────────────
    # 1) Nächster Termin: gut gefüllt, ein Mitglied bringt einen Gast mit,
    #    dazu ein Link-Gast — es bleiben Plätze frei.
    nxt = future[0]
    for i in [0, 1, 3]:
        db.add(
            Booking(
                event_id=nxt.id,
                member_id=members[i].id,
                guest_count=1 if i == 1 else 0,
            )
        )
    db.add(
        GuestBooking(
            event_id=nxt.id,
            name="Kim Roth",
            email="kim.roth@example.com",
            count=1,
        )
    )
    db.commit()
    # Offene Gast-Anfrage: über den Link gebucht, als die Mindestzahl schon
    # erreicht war — wartet auf Bestätigung durch ein Super-Mitglied.
    # (confirmed_at erst nach dem Flush leeren, der Default greift sonst.)
    request = GuestBooking(
        event_id=nxt.id, name="Jana Pohl", email="jana.pohl@example.com", count=1
    )
    db.add(request)
    db.flush()
    request.confirmed_at = None
    # Abo-Gastlink: nächster Termin knapp außerhalb des Buchungsfensters,
    # damit die Seite „buchbar ab …“ zeigt (normalerweise 5 Tage).
    sub.guest_days_ahead = max(0, min(5, (nxt.date - today).days - 1))
    db.commit()

    # 2) Übernächster Termin: ausgebucht, mit Warteliste.
    full = future[1]
    for i in range(6):
        db.add(
            Booking(
                event_id=full.id,
                member_id=members[i].id,
                guest_count=1 if i in (0, 2) else 0,
            )
        )
    db.commit()
    for i in (6, 7):
        db.add(WaitlistEntry(event_id=full.id, member_id=members[i].id))
    db.commit()

    # 3) Dritter Termin: läuft gerade an, eine offene Storno-Anfrage
    #    (Mitglied wollte nach Ablauf der freien Frist abmelden).
    third = future[2]
    for i in [0, 2, 4, 5]:
        db.add(Booking(event_id=third.id, member_id=members[i].id))
    db.commit()
    req = (
        db.query(Booking)
        .filter(Booking.event_id == third.id, Booking.member_id == members[4].id)
        .first()
    )
    req.cancel_requested_at = utcnow()
    db.commit()

    # 4) Vierter Termin: noch unter der Mindestteilnehmerzahl.
    thin = future[3]
    for i in [1, 3]:
        db.add(Booking(event_id=thin.id, member_id=members[i].id))
    db.commit()

    # 5) Ein Zusatztermin außerhalb des Abo-Preises (eigenes Budget,
    #    alle zahlen den gleichen Anteil) — an einem Donnerstag, damit er
    #    nicht mit einem regulären Dienstag kollidiert.
    extra_date = next_weekday(today + timedelta(days=7), 3)
    extra = services.create_extra_event(
        db,
        sub,
        extra_date,
        time(19, 0),
        90,
        Decimal("45.00"),
        max_participants=8,
        min_participants=4,
    )
    for i in [0, 1, 2]:
        db.add(Booking(event_id=extra.id, member_id=members[i].id))
    db.commit()

    # ── Zweites Abo: Jonas spielt auch freitags in der Halle ─────────────
    # Zeigt im Handbuch den Abo-Umschalter und die abo-übergreifende
    # Liste „Meine Anmeldungen“.
    hall_first = next_weekday(today - timedelta(days=1), 4)  # heute zählt mit
    hall = Subscription(
        name="Hallenvolleyball Freitag",
        weekday=4,
        start_time=time(20, 0),
        duration_minutes=DURATION,
        start_date=hall_first,
        end_date=hall_first + timedelta(days=7 * 3),
        abo_price=Decimal("160.00"),
        default_price=Decimal("200.00"),
        min_participants=4,
        max_participants=12,
    )
    db.add(hall)
    db.flush()
    hall_members = [
        Member(subscription_id=hall.id, name=name, email=email, is_super=False)
        for name, email, _, _ in (PEOPLE[1], PEOPLE[2], PEOPLE[5])
    ]
    db.add_all(hall_members)
    hall_events = []
    for w in range(4):
        d = hall_first + timedelta(days=7 * w)
        end_dt = datetime.combine(d, time(20, 0)) + timedelta(minutes=DURATION)
        ev = Event(
            subscription_id=hall.id,
            date=d,
            start_time=time(20, 0),
            end_time=end_dt.time(),
            max_participants=hall.max_participants,
            min_participants=hall.min_participants,
        )
        db.add(ev)
        hall_events.append(ev)
    db.flush()
    db.refresh(hall)
    services.recompute_budgets(db, hall)
    for ev in hall_events[:3]:
        for m in hall_members:
            db.add(Booking(event_id=ev.id, member_id=m.id))
    db.commit()

    guest_token = nxt.public_token
    print(f"Demo-DB:      {DB_PATH}")
    print(f"Abo:          {sub.name} ({sub.id})")
    print(f"Super:        {members[0].email}")
    print(f"Mitglied:     {members[1].email}")
    print(f"Minus-Konto:  {members[7].email} ({members[7].credit} €)")
    print(f"Gast-Link:    /g/{guest_token}")
    print(f"Abo-Gastlink: /g/abo/{sub.guest_token}")
    db.close()


if __name__ == "__main__":
    main()
