"""Super-Mitglieder entfernen Anmeldungen vor der Abrechnung (z. B. wer
nicht erschienen ist): nicht sich selbst, bezahlte Gäste erst nach
Zurücknehmen, nach Terminbeginn nicht unter die Mindestzahl."""

from decimal import Decimal

import pytest

from app import services
from app.models.models import Booking, GuestBooking, Member, utcnow
from tests.conftest import member_login


@pytest.fixture
def outbox(monkeypatch):
    mails = []

    async def fake_send_batch(subscription, batch):
        mails.extend({"to": m.to, "subject": m.subject, "body": m.body} for m in batch)
        return len(batch)

    monkeypatch.setattr(services, "send_batch", fake_send_batch)
    return mails


def _past_with_five(db, seed):
    """Vergangener, offener Termin (min 4) mit 5 Teilnehmern: Anna, Sina,
    Carl, Dora und ein bestätigter Linkgast."""
    event = seed["past_event"]
    event.min_participants = 4
    event.max_participants = 8
    extra = [
        Member(subscription_id=seed["sub"].id, email=f"{n.lower()}@example.com", name=n)
        for n in ("Carl", "Dora")
    ]
    db.add_all(extra)
    db.flush()
    for m in [seed["member"], seed["super_member"], *extra]:
        db.add(Booking(event_id=event.id, member_id=m.id))
    gb = GuestBooking(event_id=event.id, name="Gerd", email="gerd@example.com", count=1)
    db.add(gb)
    db.commit()
    return event, gb


def _post(client, url, csrf):
    return client.post(url, data={"csrf_token": csrf}, follow_redirects=False)


def test_super_removes_no_show_before_settlement(client, db, seed, outbox):
    event, _ = _past_with_five(db, seed)
    anna_booking = (
        db.query(Booking).filter_by(event_id=event.id, member_id=seed["member"].id).one()
    )
    csrf = member_login(client, "sina@example.com")
    page = client.get(f"/member/event/{event.id}/participants").text
    assert f"/member/booking/{anna_booking.id}/remove" in page
    assert 'class="btn btn-outline-danger btn-sm btn-icon"' in page

    resp = _post(client, f"/member/booking/{anna_booking.id}/remove", csrf)
    assert resp.status_code == 302
    assert "entfernt" in resp.headers["location"]
    db.expire_all()
    assert services.count_booked(db, event.id) == 4
    assert [m["to"] for m in outbox] == ["anna@example.com"]
    assert "Sina hat deine Anmeldung" in outbox[0]["body"]

    # Abrechnung danach: Anna zahlt nichts, die übrigen teilen durch 4
    services.settle_event(db, event)
    db.refresh(seed["member"])
    assert seed["member"].credit == Decimal("20.00")


def test_super_cannot_remove_self(client, db, seed, outbox):
    event, _ = _past_with_five(db, seed)
    own = (
        db.query(Booking)
        .filter_by(event_id=event.id, member_id=seed["super_member"].id)
        .one()
    )
    csrf = member_login(client, "sina@example.com")
    assert f"/member/booking/{own.id}/remove" not in client.get(
        f"/member/event/{event.id}/participants"
    ).text
    resp = _post(client, f"/member/booking/{own.id}/remove", csrf)
    assert "selbst" in resp.headers["location"] or "selbst" in client.get(
        resp.headers["location"]
    ).text
    db.expire_all()
    assert db.get(Booking, own.id) is not None
    assert outbox == []


def test_after_start_not_below_minimum(client, db, seed, outbox):
    event, gb = _past_with_five(db, seed)
    carl = db.query(Member).filter_by(name="Carl").one()
    dora = db.query(Member).filter_by(name="Dora").one()
    csrf = member_login(client, "sina@example.com")
    _post(client, f"/member/guest-booking/{gb.id}/remove", csrf)  # 5 → 4
    db.expire_all()
    assert services.count_booked(db, event.id) == 4
    assert [m["to"] for m in outbox] == ["gerd@example.com"]

    carl_booking = db.query(Booking).filter_by(event_id=event.id, member_id=carl.id).one()
    page = client.get(f"/member/event/{event.id}/participants").text
    assert f"/member/booking/{carl_booking.id}/remove" not in page
    resp = _post(client, f"/member/booking/{carl_booking.id}/remove", csrf)
    assert resp.status_code == 302
    db.expire_all()
    assert services.count_booked(db, event.id) == 4
    assert db.query(Booking).filter_by(member_id=dora.id).count() == 1


def test_before_start_below_minimum_allowed(client, db, seed, outbox):
    event = seed["event"]  # in 3 Tagen
    event.min_participants = 2
    db.add_all(
        [
            Booking(event_id=event.id, member_id=seed["member"].id),
            Booking(event_id=event.id, member_id=seed["super_member"].id),
        ]
    )
    db.commit()
    anna = db.query(Booking).filter_by(event_id=event.id, member_id=seed["member"].id).one()
    csrf = member_login(client, "sina@example.com")
    _post(client, f"/member/booking/{anna.id}/remove", csrf)
    db.expire_all()
    assert services.count_booked(db, event.id) == 1
    # Entfernt-Mail an Anna + Mitspieler-Suche an Sina
    subjects = sorted(m["subject"].split(":")[0] for m in outbox)
    assert subjects == ["Abgemeldet", "Mitspieler gesucht"]


def test_paid_and_pending_guests_and_settled_events_are_protected(client, db, seed, outbox):
    event, gb = _past_with_five(db, seed)
    gb.paid_at = utcnow()
    gb.paid_amount = Decimal("5.00")
    pending = GuestBooking(event_id=event.id, name="Paul", email="", count=1)
    db.add(pending)
    db.flush()
    pending.confirmed_at = None
    db.commit()
    csrf = member_login(client, "sina@example.com")
    page = client.get(f"/member/event/{event.id}/participants").text
    assert f"/member/guest-booking/{gb.id}/remove" not in page
    assert f"/member/guest-booking/{pending.id}/remove" not in page
    _post(client, f"/member/guest-booking/{gb.id}/remove", csrf)
    _post(client, f"/member/guest-booking/{pending.id}/remove", csrf)
    db.expire_all()
    assert db.get(GuestBooking, gb.id) and db.get(GuestBooking, pending.id)

    event.settled_at = utcnow()
    db.commit()
    carl = db.query(Member).filter_by(name="Carl").one()
    carl_booking = db.query(Booking).filter_by(event_id=event.id, member_id=carl.id).one()
    assert "/remove" not in client.get(f"/member/event/{event.id}/participants").text
    _post(client, f"/member/booking/{carl_booking.id}/remove", csrf)
    db.expire_all()
    assert db.get(Booking, carl_booking.id) is not None
    assert outbox == []


def test_regular_member_and_other_abo_cannot_remove(client, db, seed, outbox):
    event, _ = _past_with_five(db, seed)
    carl = db.query(Member).filter_by(name="Carl").one()
    carl_booking = db.query(Booking).filter_by(event_id=event.id, member_id=carl.id).one()
    csrf = member_login(client)  # Anna, kein Super
    assert "/remove" not in client.get(f"/member/event/{event.id}/participants").text
    resp = _post(client, f"/member/booking/{carl_booking.id}/remove", csrf)
    assert resp.status_code == 403

    # Super eines anderen Abos
    other = Member(
        subscription_id=seed["other_sub"].id, email="olga@example.com",
        name="Olga", is_super=True,
    )
    db.add(other)
    db.commit()
    client.cookies.clear()
    csrf = member_login(client, "olga@example.com")
    _post(client, f"/member/booking/{carl_booking.id}/remove", csrf)
    db.expire_all()
    assert db.get(Booking, carl_booking.id) is not None
