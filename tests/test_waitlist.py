"""Warteliste: beitreten, nachrücken bei frei werdenden Plätzen."""

from conftest import admin_login, member_login

from app.models.models import Booking, GuestBooking, Member, WaitlistEntry


def _fill_event(db, seed):
    """Event (max 4) komplett füllen: Sina + 3er-Gastbuchung."""
    event = seed["event"]
    db.add(Booking(event_id=event.id, member_id=seed["super_member"].id))
    db.add(GuestBooking(event_id=event.id, name="G", email="g@x.de", count=3))
    db.commit()
    return event


def test_join_waitlist_only_when_full(client, db, seed):
    csrf = member_login(client)  # Anna
    event = seed["event"]
    # Noch Plätze frei → abgelehnt
    resp = client.post(
        f"/member/event/{event.id}/waitlist",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "noch%20Pl%C3%A4tze%20frei" in resp.headers["location"]

    _fill_event(db, seed)
    resp = client.post(
        f"/member/event/{event.id}/waitlist",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "Platz%201" in resp.headers["location"]
    assert db.query(WaitlistEntry).count() == 1

    # Doppelt geht nicht
    resp = client.post(
        f"/member/event/{event.id}/waitlist",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "bereits" in resp.headers["location"]

    # Dashboard zeigt Position
    resp = client.get("/member/dashboard")
    assert "Warteliste Platz 1" in resp.text


def test_promotion_on_unbook(client, db, seed):
    """Sina meldet sich ab → Anna rückt automatisch nach."""
    event = _fill_event(db, seed)
    db.add(WaitlistEntry(event_id=event.id, member_id=seed["member"].id))
    db.commit()

    csrf = member_login(client, email="sina@example.com")
    resp = client.post(
        f"/member/event/{event.id}/unbook",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "Anna%20r%C3%BCckt" in resp.headers["location"]
    db.expire_all()
    booking = (
        db.query(Booking)
        .filter(
            Booking.event_id == event.id,
            Booking.member_id == seed["member"].id,
        )
        .one()
    )
    assert booking.guest_count == 0
    assert db.query(WaitlistEntry).count() == 0


def test_promotion_on_admin_removal_and_capacity(client, db, seed):
    event = _fill_event(db, seed)
    db.add(WaitlistEntry(event_id=event.id, member_id=seed["member"].id))
    db.commit()
    csrf = admin_login(client)

    # Kapazität erhöhen → Nachrücken
    resp = client.post(
        f"/admin/event/{event.id}/capacity",
        data={"min_participants": "1", "max_participants": "5", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert "nachger%C3%BCckt" in resp.headers["location"]
    db.expire_all()
    assert db.query(WaitlistEntry).count() == 0
    assert (
        db.query(Booking)
        .filter(Booking.event_id == event.id, Booking.member_id == seed["member"].id)
        .count()
        == 1
    )


def test_leave_waitlist(client, db, seed):
    event = _fill_event(db, seed)
    db.add(WaitlistEntry(event_id=event.id, member_id=seed["member"].id))
    db.commit()
    csrf = member_login(client)
    resp = client.post(
        f"/member/event/{event.id}/waitlist/leave",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "ausgetragen" in resp.headers["location"]
    assert db.query(WaitlistEntry).count() == 0


def test_no_promotion_into_cancelled_event(client, db, seed):
    event = _fill_event(db, seed)
    db.add(WaitlistEntry(event_id=event.id, member_id=seed["member"].id))
    event.is_cancelled = True
    db.commit()

    import pytest
    from app import services

    import asyncio

    promoted = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        services.promote_from_waitlist(db, event)
    )
    assert promoted == []
    assert db.query(WaitlistEntry).count() == 1
