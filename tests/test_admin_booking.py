"""Admin meldet Mitglieder und Gäste bei Terminen an/ab."""

from conftest import admin_login

from app.models.models import Booking, GuestBooking


def test_admin_books_member_with_guests(client, db, seed):
    csrf = admin_login(client)
    event = seed["event"]
    resp = client.post(
        f"/admin/event/{event.id}/book-member",
        data={"member_id": seed["member"].id, "guest_count": "1", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "angemeldet" in resp.headers["location"]
    booking = db.query(Booking).one()
    assert booking.member_id == seed["member"].id
    assert booking.guest_count == 1

    # Doppelte Anmeldung abgelehnt
    resp = client.post(
        f"/admin/event/{event.id}/book-member",
        data={"member_id": seed["member"].id, "guest_count": "0", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert "bereits" in resp.headers["location"]
    assert db.query(Booking).count() == 1


def test_admin_cannot_book_member_of_other_subscription(client, db, seed):
    csrf = admin_login(client)
    resp = client.post(
        f"/admin/event/{seed['event'].id}/book-member",
        data={"member_id": seed["outsider"].id, "guest_count": "0", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert "Mitglied%20nicht%20gefunden" in resp.headers["location"]
    assert db.query(Booking).count() == 0


def test_admin_books_guest_and_capacity_enforced(client, db, seed):
    csrf = admin_login(client)
    event = seed["event"]  # max 4
    resp = client.post(
        f"/admin/event/{event.id}/book-guest",
        data={"name": "Gast Eins", "email": "", "count": "3", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert "angemeldet" in resp.headers["location"]
    # 3 belegt, 2 weitere passen nicht mehr
    resp = client.post(
        f"/admin/event/{event.id}/book-guest",
        data={"name": "Gast Zwei", "email": "", "count": "2", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert "ausgebucht" in resp.headers["location"]
    assert db.query(GuestBooking).count() == 1


def test_admin_removes_bookings(client, db, seed):
    csrf = admin_login(client)
    event = seed["event"]
    booking = Booking(event_id=event.id, member_id=seed["member"].id)
    gb = GuestBooking(event_id=event.id, name="G", email="g@x.de", count=1)
    db.add_all([booking, gb])
    db.commit()

    resp = client.post(
        f"/admin/booking/{booking.id}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "entfernt" in resp.headers["location"]
    resp = client.post(
        f"/admin/guest-booking/{gb.id}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "entfernt" in resp.headers["location"]
    db.expire_all()
    assert db.query(Booking).count() == 0
    assert db.query(GuestBooking).count() == 0


def test_admin_booking_blocked_on_settled_event(client, db, seed):
    from app.models.models import utcnow

    csrf = admin_login(client)
    event = seed["past_event"]
    booking = Booking(event_id=event.id, member_id=seed["member"].id)
    db.add(booking)
    event.settled_at = utcnow()
    db.commit()

    resp = client.post(
        f"/admin/event/{event.id}/book-guest",
        data={"name": "Zu spät", "email": "", "count": "1", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert "abgerechnet" in resp.headers["location"]
    resp = client.post(
        f"/admin/booking/{booking.id}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "abgerechnet" in resp.headers["location"]
    assert db.query(Booking).count() == 1


def test_event_page_shows_booking_forms(client, seed):
    admin_login(client)
    resp = client.get(f"/admin/event/{seed['event'].id}")
    assert resp.status_code == 200
    assert "Mitglied anmelden" in resp.text
    assert "Gast anmelden" in resp.text
    assert "Anna" in resp.text  # im Auswahlfeld
