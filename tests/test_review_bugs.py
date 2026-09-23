"""Regressionstests für die Bugs aus dem Code-Review."""

from datetime import date, timedelta

from conftest import admin_login, get_csrf, member_login

from app.models.models import Booking, Event, GuestBooking, Subscription, utcnow


def _sub_form(**overrides):
    form = {
        "name": "Neu",
        "weekday": "2",
        "start_time": "18:30",
        "duration_minutes": "120",
        "start_date": "2026-10-01",
        "end_date": "2026-12-31",
        "default_price": "100",
        "abo_price": "80",
        "max_participants": "12",
        "min_participants": "4",
    }
    form.update(overrides)
    return form


def test_admin_cancel_twice_does_not_reactivate(client, db, seed):
    """Doppeltipp auf „Absagen“ hat den Termin früher wieder aktiviert."""
    event = seed["event"]
    csrf = admin_login(client)
    for _ in range(2):
        client.post(
            f"/admin/event/{event.id}/cancel",
            data={"reduce_price": "no", "csrf_token": csrf},
        )
    db.expire_all()
    assert db.get(Event, event.id).is_cancelled
    client.post(f"/admin/event/{event.id}/reactivate", data={"csrf_token": csrf})
    db.expire_all()
    assert not db.get(Event, event.id).is_cancelled


def test_guest_booking_redirects_to_confirmation(client, db, seed):
    """Post/Redirect/Get: Neuladen der Bestätigung bucht nicht doppelt."""
    event = seed["event"]
    csrf = get_csrf(client, f"/g/{event.public_token}")
    resp = client.post(
        f"/g/{event.public_token}/book",
        data={"name": "Gast", "email": "gast@example.com", "count": 1, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    url = resp.headers["location"]
    assert f"/g/{event.public_token}/buchung/" in url
    for _ in range(2):
        assert "Danke, Gast" in client.get(url).text
    assert db.query(GuestBooking).count() == 1


def test_guest_booking_validates_input(client, db, seed):
    event = seed["event"]
    csrf = get_csrf(client, f"/g/{event.public_token}")
    for data in ({"name": " ", "email": "a@b.de"}, {"name": "X", "email": "kein-mail"}):
        client.post(f"/g/{event.public_token}/book", data={**data, "csrf_token": csrf})
    assert db.query(GuestBooking).count() == 0


def test_no_booking_on_settled_event(client, db, seed):
    """Am Abrechnungstag konnte man früher noch nachbuchen (nie belastet)."""
    event = seed["event"]
    event.settled_at = utcnow()
    db.commit()
    csrf = member_login(client)
    client.post(
        f"/member/event/{event.id}/book", data={"guest_count": 0, "csrf_token": csrf}
    )
    client.post(
        f"/g/{event.public_token}/book",
        data={"name": "G", "email": "g@x.de", "count": 1, "csrf_token": csrf},
    )
    assert db.query(Booking).filter(Booking.event_id == event.id).count() == 0
    assert db.query(GuestBooking).count() == 0


def test_subscription_form_validation(client, db, seed):
    csrf = admin_login(client)
    before = db.query(Subscription).count()
    for bad in (
        {"start_date": "kaputt"},
        {"start_date": "2026-12-31", "end_date": "2026-10-01"},
        {"min_participants": "20", "max_participants": "10"},
    ):
        resp = client.post(
            "/admin/subscription/new", data={**_sub_form(**bad), "csrf_token": csrf}
        )
        assert resp.status_code == 400  # Formular mit Fehlermeldung, kein 500
    assert db.query(Subscription).count() == before


def test_subscription_accepts_time_input(client, db, seed):
    csrf = admin_login(client)
    client.post("/admin/subscription/new", data={**_sub_form(), "csrf_token": csrf})
    sub = db.query(Subscription).filter(Subscription.name == "Neu").one()
    assert sub.start_time.strftime("%H:%M") == "18:30"


def test_extra_event_min_above_max_refused(client, db, seed):
    csrf = admin_login(client)
    sub_id = seed["event"].subscription_id
    day = (date.today() + timedelta(days=10)).isoformat()
    client.post(
        f"/admin/subscription/{sub_id}/extra-event",
        data={
            "event_date": day, "start_time": "19:00", "budget": "40",
            "min_participants": "9", "max_participants": "4", "csrf_token": csrf,
        },
    )
    assert db.query(Event).filter(Event.is_extra).count() == 0
    client.post(
        f"/admin/subscription/{sub_id}/extra-event",
        data={
            "event_date": day, "start_time": "19:00", "budget": "40",
            "min_participants": "2", "max_participants": "4", "csrf_token": csrf,
        },
    )
    extra = db.query(Event).filter(Event.is_extra).one()
    assert extra.start_time.strftime("%H:%M") == "19:00"


# ── UI-Regeln ──────────────────────────────────────────────────────────────


def test_money_inputs_accept_cents():
    """step="0.50" ließ Beträge wie 2,67 € nicht zu (Browser-Validierung)."""
    from pathlib import Path

    for path in Path("app/templates").rglob("*.html"):
        assert 'step="0.5' not in path.read_text(), path


def test_nav_logout_is_not_called_abmelden(client, seed):
    member_login(client)
    html = client.get("/member/dashboard").text
    assert '<a href="/member/logout">Logout</a>' in html


def test_time_inputs_instead_of_hour_minute_fields(client, seed):
    admin_login(client)
    html = client.get("/admin/subscription/new").text
    assert 'type="time"' in html
    assert 'name="start_hour"' not in html
