"""Abmeldungen: Suchaufruf nur beim Unterschreiten nach Wartelisten-Nachrücken."""

from datetime import datetime, timedelta

import pytest

from conftest import admin_login, member_login
from app import services
from app.models.models import Booking, GuestBooking, Member, WaitlistEntry, utcnow
from app.routes import member as member_routes


@pytest.fixture
def outbox(monkeypatch):
    mails = []

    async def capture(subscription, batch):
        mails.extend(batch)
        return len(batch)

    monkeypatch.setattr(services, "send_batch", capture)
    monkeypatch.setattr(member_routes, "send_batch", capture)
    return mails


def _seed_players(db, seed, minimum=4):
    event = seed["event"]
    event.min_participants = minimum
    event.max_participants = 8
    leaving = Booking(event_id=event.id, member_id=seed["member"].id, guest_count=1)
    remaining = Member(subscription_id=seed["sub"].id, name="Rita", email="rita@example.com")
    inactive = Member(subscription_id=seed["sub"].id, name="Inaktiv", email="inactive@example.com", is_active=False, is_super=True)
    db.add_all([remaining, inactive, leaving])
    db.flush()
    db.add(Booking(event_id=event.id, member_id=remaining.id))
    db.add(GuestBooking(event_id=event.id, name="Gustav", email="gustav@example.com"))
    db.commit()
    return event, leaving


@pytest.mark.parametrize("action", ["free", "approve", "admin_member", "admin_guest"])
def test_cancellation_crossing_minimum_mails_remaining_players_and_supers(client, db, seed, outbox, action):
    event, leaving = _seed_players(db, seed)
    if action == "free":
        csrf = member_login(client)
        path = f"/member/event/{event.id}/unbook"
    elif action == "approve":
        leaving.cancel_requested_at = utcnow()
        db.commit()
        csrf = member_login(client, email="sina@example.com")
        path = f"/member/cancel-request/{leaving.id}/approve"
    else:
        csrf = admin_login(client)
        if action == "admin_member":
            path = f"/admin/booking/{leaving.id}/delete"
        else:
            gb = db.query(GuestBooking).filter_by(event_id=event.id).one()
            path = f"/admin/guest-booking/{gb.id}/delete"
    response = client.post(path, data={"csrf_token": csrf}, follow_redirects=False)
    assert response.status_code == 302
    expected = {"sina@example.com", "rita@example.com", "gustav@example.com"}
    if action == "admin_guest":
        expected = {"sina@example.com", "rita@example.com", "anna@example.com"}
    assert {mail.to for mail in outbox} == expected
    for mail in outbox:
        assert seed["sub"].name in mail.subject
        assert event.date.strftime("%d.%m.%Y") in mail.body
        assert event.start_time.strftime("%H:%M") in mail.body
    db.expire_all()
    assert services.count_booked(db, event.id) == (3 if action == "admin_guest" else 2)
    # Double taps must not resend the search request.
    client.post(path, data={"csrf_token": csrf}, follow_redirects=False)
    assert len(outbox) == len(expected)


@pytest.mark.parametrize("condition", ["already_below", "still_enough", "waitlist", "cancelled", "settled", "started"])
def test_no_search_mail_when_minimum_transition_does_not_need_recruiting(client, db, seed, outbox, condition):
    minimum = {"already_below": 5, "still_enough": 2}.get(condition, 4)
    event, leaving = _seed_players(db, seed, minimum=minimum)
    if condition == "waitlist":
        replacement = Member(subscription_id=seed["sub"].id, name="Ersatz", email="ersatz@example.com")
        db.add(replacement)
        db.flush()
        db.add_all([WaitlistEntry(event_id=event.id, member_id=replacement.id), WaitlistEntry(event_id=event.id, member_id=seed["super_member"].id)])
    elif condition == "cancelled":
        event.is_cancelled = True
    elif condition == "settled":
        event.settled_at = utcnow()
    elif condition == "started":
        event.date -= timedelta(days=4)
    db.commit()
    csrf = admin_login(client)
    client.post(f"/admin/booking/{leaving.id}/delete", data={"csrf_token": csrf}, follow_redirects=False)
    if condition == "waitlist":
        assert services.count_booked(db, event.id) == 4
        assert {mail.to for mail in outbox} == {"ersatz@example.com", "sina@example.com"}
        assert len(outbox) == 2
    else:
        assert outbox == []


def test_cancel_request_alone_does_not_send_search_mail(client, db, seed, outbox):
    event, leaving = _seed_players(db, seed)
    seed["sub"].cancel_hours_free = 120
    db.commit()
    csrf = member_login(client)
    client.post(f"/member/event/{event.id}/unbook", data={"csrf_token": csrf}, follow_redirects=False)
    db.expire_all()
    assert db.get(Booking, leaving.id).cancel_requested_at is not None
    assert services.count_booked(db, event.id) == 4
    assert len(outbox) == 1
    assert outbox[0].to == "sina@example.com"


def test_search_recipients_are_active_scoped_confirmed_and_deduplicated(client, db, seed, outbox):
    event, leaving = _seed_players(db, seed, minimum=9)
    event.max_participants = 12
    seed["outsider"].is_super = True
    inactive = db.query(Member).filter_by(email="inactive@example.com").one()
    duplicate_guest = GuestBooking(event_id=event.id, name="Rita als Gast", email=" RITA@EXAMPLE.COM ")
    pending_guest = GuestBooking(event_id=event.id, name="Anfrage", email="pending@example.com")
    db.add_all([
        Booking(event_id=event.id, member_id=seed["super_member"].id),
        Booking(event_id=event.id, member_id=inactive.id),
        duplicate_guest,
        pending_guest,
        GuestBooking(event_id=event.id, name="Ohne Mail", email=" "),
    ])
    db.flush()
    pending_guest.confirmed_at = None
    db.commit()
    csrf = admin_login(client)
    response = client.post(f"/admin/booking/{leaving.id}/delete", data={"csrf_token": csrf}, follow_redirects=False)
    assert response.status_code == 302
    assert len(outbox) == 3
    assert {mail.to for mail in outbox} == {"sina@example.com", "rita@example.com", "gustav@example.com"}
    assert "Rita" in next(mail.body for mail in outbox if mail.to == "rita@example.com")
    assert services.count_booked(db, event.id) == 7


@pytest.mark.parametrize("role", ["admin", "super"])
def test_rejecting_pending_reservation_crosses_minimum_but_excludes_other_pending_guests(client, db, seed, outbox, role):
    event, leaving = _seed_players(db, seed, minimum=5)
    rejected = db.query(GuestBooking).filter_by(event_id=event.id).one()
    pending = GuestBooking(event_id=event.id, name="Noch offen", email="pending@example.com")
    db.add(pending)
    db.flush()
    rejected.confirmed_at = None
    pending.confirmed_at = None
    db.commit()
    if role == "admin":
        csrf = admin_login(client)
        path = f"/admin/guest-booking/{rejected.id}/reject"
    else:
        csrf = member_login(client, email="sina@example.com")
        path = f"/member/guest-booking/{rejected.id}/reject"
    response = client.post(path, data={"csrf_token": csrf}, follow_redirects=False)
    assert response.status_code == 302
    # The rejected guest gets only their existing rejection mail, not a search request.
    assert len(outbox) == 4
    assert {mail.to for mail in outbox} == {"anna@example.com", "sina@example.com", "rita@example.com", "gustav@example.com"}
    assert services.count_booked(db, event.id) == 4
    client.post(path, data={"csrf_token": csrf}, follow_redirects=False)
    assert len(outbox) == 4


def test_leaving_active_super_still_gets_search_request(client, db, seed, outbox):
    event, leaving = _seed_players(db, seed)
    seed["member"].is_super = True
    db.commit()
    csrf = member_login(client)
    response = client.post(f"/member/event/{event.id}/unbook", data={"csrf_token": csrf}, follow_redirects=False)
    assert response.status_code == 302
    assert len(outbox) == 4
    assert {mail.to for mail in outbox} == {"anna@example.com", "sina@example.com", "rita@example.com", "gustav@example.com"}


@pytest.mark.parametrize("offset", [timedelta(), timedelta(minutes=1)])
def test_no_search_request_at_or_after_start_on_same_day(client, db, seed, outbox, monkeypatch, offset):
    from app import clock

    event, leaving = _seed_players(db, seed)
    start = datetime.combine(event.date, event.start_time)
    monkeypatch.setattr(clock, "now", lambda session: start + offset)
    csrf = admin_login(client)
    response = client.post(f"/admin/booking/{leaving.id}/delete", data={"csrf_token": csrf}, follow_redirects=False)
    assert response.status_code == 302
    assert outbox == []


def test_waitlist_partial_replacement_is_included_in_search_request(client, db, seed, outbox):
    event, leaving = _seed_players(db, seed)
    replacement = Member(subscription_id=seed["sub"].id, name="Ersatz", email="ersatz@example.com")
    db.add(replacement)
    db.flush()
    db.add(WaitlistEntry(event_id=event.id, member_id=replacement.id))
    db.commit()
    csrf = admin_login(client)
    response = client.post(f"/admin/booking/{leaving.id}/delete", data={"csrf_token": csrf}, follow_redirects=False)
    assert response.status_code == 302
    assert services.count_booked(db, event.id) == 3
    assert len(outbox) == 5
    assert [mail.to for mail in outbox].count("ersatz@example.com") == 2
    assert {mail.to for mail in outbox} == {"ersatz@example.com", "sina@example.com", "rita@example.com", "gustav@example.com"}
