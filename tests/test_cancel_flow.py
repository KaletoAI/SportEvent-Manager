"""Stornierungsfristen: frei / mit Super-Freigabe / gesperrt."""

from datetime import date, timedelta

from conftest import member_login

from app.models.models import Booking, Event


def _book(db, seed, days_ahead: int) -> Event:
    """Eigenes Event in N Tagen + Anmeldung von Anna."""
    event = Event(
        subscription_id=seed["sub"].id,
        date=date.today() + timedelta(days=days_ahead),
        start_time=seed["event"].start_time,
        end_time=seed["event"].end_time,
        max_participants=8,
        min_participants=1,
    )
    db.add(event)
    db.flush()
    db.add(Booking(event_id=event.id, member_id=seed["member"].id))
    db.commit()
    return event


def _unbook(client, csrf, event_id):
    return client.post(
        f"/member/event/{event_id}/unbook",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )


def test_free_cancellation_outside_deadline(client, db, seed):
    # cancel_hours_free = 48 (Default) → 5 Tage vorher: frei
    event = _book(db, seed, days_ahead=5)
    csrf = member_login(client)
    resp = _unbook(client, csrf, event.id)
    assert "Abmeldung%20erfolgt" in resp.headers["location"]
    assert db.query(Booking).filter(Booking.event_id == event.id).count() == 0


def test_cancellation_inside_window_needs_approval(client, db, seed):
    # ~24h vorher: zwischen approval (0h) und free (48h) → Anfrage
    event = _book(db, seed, days_ahead=1)
    csrf = member_login(client)
    resp = _unbook(client, csrf, event.id)
    assert "Super-Mitglieder" in resp.headers["location"]
    booking = db.query(Booking).filter(Booking.event_id == event.id).one()
    assert booking.cancel_requested_at is not None

    # Doppelte Anfrage abgelehnt
    resp = _unbook(client, csrf, event.id)
    assert "bereits" in resp.headers["location"]


def test_cancellation_blocked_after_approval_deadline(client, db, seed):
    sub = seed["sub"]
    sub.cancel_hours_free = 120
    sub.cancel_hours_approval = 72
    db.commit()
    event = _book(db, seed, days_ahead=1)  # ~24h < 72h → gesperrt
    csrf = member_login(client)
    resp = _unbook(client, csrf, event.id)
    assert "nicht%20mehr%20m%C3%B6glich" in resp.headers["location"]
    booking = db.query(Booking).filter(Booking.event_id == event.id).one()
    assert booking.cancel_requested_at is None


def test_super_approves_cancel_request(client, db, seed):
    event = _book(db, seed, days_ahead=1)
    csrf = member_login(client)
    _unbook(client, csrf, event.id)  # erzeugt Anfrage
    booking = db.query(Booking).filter(Booking.event_id == event.id).one()

    client.get("/member/logout")
    super_csrf = member_login(client, email="sina@example.com")
    # Anfrage sichtbar im Dashboard
    resp = client.get("/member/dashboard")
    assert "Storno-Anfrage" in resp.text
    resp = client.post(
        f"/member/cancel-request/{booking.id}/approve",
        data={"csrf_token": super_csrf},
        follow_redirects=False,
    )
    assert "freigegeben" in resp.headers["location"]
    db.expire_all()
    assert db.query(Booking).filter(Booking.event_id == event.id).count() == 0


def test_super_rejects_cancel_request(client, db, seed):
    event = _book(db, seed, days_ahead=1)
    csrf = member_login(client)
    _unbook(client, csrf, event.id)
    booking = db.query(Booking).filter(Booking.event_id == event.id).one()

    client.get("/member/logout")
    super_csrf = member_login(client, email="sina@example.com")
    resp = client.post(
        f"/member/cancel-request/{booking.id}/reject",
        data={"csrf_token": super_csrf},
        follow_redirects=False,
    )
    assert "abgelehnt" in resp.headers["location"]
    db.expire_all()
    booking = db.query(Booking).filter(Booking.event_id == event.id).one()
    assert booking.cancel_requested_at is None  # Anmeldung bleibt bestehen


def test_normal_member_cannot_approve(client, db, seed):
    event = _book(db, seed, days_ahead=1)
    csrf = member_login(client)
    _unbook(client, csrf, event.id)
    booking = db.query(Booking).filter(Booking.event_id == event.id).one()
    resp = client.post(
        f"/member/cancel-request/{booking.id}/approve",
        data={"csrf_token": csrf},
    )
    assert resp.status_code == 403
