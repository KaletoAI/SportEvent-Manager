"""Booking: authorization, capacity, validation, guest links."""

from conftest import admin_login, get_csrf, member_login

from app.models.models import Booking, GuestBooking


def _book(client, event_id, csrf, guest_count=0):
    return client.post(
        f"/member/event/{event_id}/book",
        data={"guest_count": str(guest_count), "csrf_token": csrf},
        follow_redirects=False,
    )


def test_member_can_book_and_unbook(client, db, seed):
    csrf = member_login(client)
    event = seed["event"]

    resp = _book(client, event.id, csrf, guest_count=1)
    assert resp.status_code == 302
    assert "best%C3%A4tigt" in resp.headers["location"]
    assert db.query(Booking).count() == 1

    resp = client.post(
        f"/member/event/{event.id}/unbook",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(Booking).count() == 0


def test_member_cannot_book_other_subscriptions_event(client, db, seed):
    """AuthZ fix S3: Bernd (Fußball) darf keinen Beachvolleyball-Termin buchen."""
    csrf = member_login(client, email="bernd@example.com")
    resp = _book(client, seed["event"].id, csrf)
    assert resp.status_code == 302
    assert "nicht%20gefunden" in resp.headers["location"]
    assert db.query(Booking).count() == 0


def test_negative_guest_count_rejected(client, db, seed):
    """S4: negative Werte dürfen die Kapazitätsprüfung nicht unterlaufen."""
    csrf = member_login(client)
    resp = _book(client, seed["event"].id, csrf, guest_count=-5)
    assert resp.status_code == 422
    assert db.query(Booking).count() == 0


def test_capacity_enforced_for_members(client, db, seed):
    csrf = member_login(client)
    # max_participants = 4; member + 4 guests = 5 > 4
    resp = _book(client, seed["event"].id, csrf, guest_count=4)
    assert resp.status_code == 302
    assert "ausgebucht" in resp.headers["location"]
    assert db.query(Booking).count() == 0


def test_cannot_book_past_event(client, db, seed):
    csrf = member_login(client)
    resp = _book(client, seed["past_event"].id, csrf)
    assert resp.status_code == 302
    assert db.query(Booking).count() == 0


def test_guest_booking_via_public_token(client, db, seed):
    event = seed["event"]
    csrf = get_csrf(client, f"/g/{event.public_token}")
    resp = client.post(
        f"/g/{event.public_token}/book",
        data={
            "name": "Gast",
            "email": "gast@example.com",
            "count": "2",
            "csrf_token": csrf,
        },
    )
    assert resp.status_code == 200
    assert "Danke, Gast" in resp.text
    # 2 Teilnehmer teilen sich das Normal-Budget 10 € → 2 × 5,00 = 10,00
    assert "10,00" in resp.text
    assert "pay@example.com" in resp.text
    assert db.query(GuestBooking).count() == 1


def test_guest_page_not_reachable_via_event_id(client, seed):
    """S12: die interne Event-ID darf kein gültiger Gastlink sein."""
    resp = client.get(f"/g/{seed['event'].id}")
    assert resp.status_code == 404


def test_guest_booking_validation_and_capacity(client, db, seed):
    event = seed["event"]
    csrf = get_csrf(client, f"/g/{event.public_token}")
    # count=0 / negativ → 422
    for bad in ("0", "-3"):
        resp = client.post(
            f"/g/{event.public_token}/book",
            data={"name": "G", "email": "g@x.de", "count": bad, "csrf_token": csrf},
        )
        assert resp.status_code == 422
    # Kapazität: 5 > 4
    resp = client.post(
        f"/g/{event.public_token}/book",
        data={"name": "G", "email": "g@x.de", "count": "5", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "ausgebucht" in resp.headers["location"]
    assert db.query(GuestBooking).count() == 0


def test_admin_event_page_shows_guest_link(client, seed):
    admin_login(client)
    resp = client.get(f"/admin/event/{seed['event'].id}")
    assert resp.status_code == 200
    assert f"/g/{seed['event'].public_token}" in resp.text
