"""Tab „Meine“: Anmeldungen über alle Abos derselben Person, Wochen hervorgehoben."""

import re
from datetime import date, time, timedelta
from decimal import Decimal

from app.models.models import Booking, Event, Member

from conftest import member_login


def _event(db, sub, day):
    e = Event(
        subscription_id=sub.id,
        date=day,
        start_time=time(18, 0),
        end_time=time(20, 0),
        max_participants=4,
        min_participants=1,
        abo_budget=Decimal("8.00"),
        normal_budget=Decimal("10.00"),
    )
    db.add(e)
    db.flush()
    return e


def _items(html):
    """Karten im Block „Meine Anmeldungen“: (Wochen-Klasse, Text) je Karte."""
    block = html.split('id="my-bookings"', 1)[1].split("</details>", 1)[0]
    block = block.split("<details", 1)[0]  # frühere Anmeldungen nicht mitzählen
    chunks = block.split('class="event-item booking-item ')[1:]
    return [(re.match(r"week-\w+", c).group(0), c) for c in chunks]


def test_my_bookings_span_all_abos_with_week_highlight(client, db, seed):
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    anna = seed["member"]
    anna2 = Member(
        subscription_id=seed["other_sub"].id,
        email="anna@example.com",
        name="Anna",
        password_hash="",
    )
    db.add(anna2)
    db.flush()
    this_week = _event(db, seed["sub"], today)
    next_week = _event(db, seed["other_sub"], monday + timedelta(days=9))
    later = _event(db, seed["other_sub"], monday + timedelta(days=21))
    db.add_all(
        [
            Booking(member_id=anna.id, event_id=this_week.id),
            Booking(member_id=anna2.id, event_id=next_week.id),
            Booking(member_id=anna2.id, event_id=later.id),
            Booking(member_id=anna.id, event_id=seed["past_event"].id),
            # Fremde Buchung im zweiten Abo darf nicht auftauchen
            Booking(member_id=seed["outsider"].id, event_id=later.id, guest_count=3),
        ]
    )
    db.commit()

    member_login(client)
    html = client.get("/member/dashboard").text
    items = _items(html)
    assert [cls for cls, _ in items] == ["week-current", "week-next", "week-later"]
    assert "Beachvolleyball" in items[0][1]
    assert "Fußball" in items[1][1] and "Fußball" in items[2][1]
    assert "Diese Woche" in html and "Nächste Woche" in html
    # Vergangene Anmeldung landet im eingeklappten Bereich, nicht in der Wochenliste
    assert "Frühere Anmeldungen (1)" in html


def test_my_bookings_ignores_inactive_other_membership(client, db, seed):
    anna2 = Member(
        subscription_id=seed["other_sub"].id,
        email="anna@example.com",
        name="Anna",
        password_hash="",
        is_active=False,
    )
    db.add(anna2)
    db.flush()
    e = _event(db, seed["other_sub"], date.today() + timedelta(days=2))
    db.add(Booking(member_id=anna2.id, event_id=e.id))
    db.commit()

    member_login(client)
    html = client.get("/member/dashboard").text
    assert _items(html) == []
    assert "Keine kommenden Anmeldungen" in html
