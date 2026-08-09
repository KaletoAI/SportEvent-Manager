"""System-Datum mit Admin-Override (Test-/Debug-Funktion)."""

from datetime import date, timedelta

from conftest import admin_login

from app import clock
from app.models.models import Booking, Payment


def test_today_defaults_to_real_date(db):
    assert clock.today(db) == date.today()
    assert clock.get_override(db) is None


def test_override_set_and_reset(client, db):
    csrf = admin_login(client)
    target = (date.today() + timedelta(days=30)).isoformat()

    resp = client.post(
        "/admin/test-date",
        data={"test_date": target, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    db.expire_all()
    assert clock.today(db).isoformat() == target

    # Banner sichtbar
    resp = client.get("/admin/dashboard")
    assert "Test-Datum aktiv" in resp.text

    # Zurücksetzen (leeres Feld)
    client.post(
        "/admin/test-date", data={"test_date": "", "csrf_token": csrf}
    )
    db.expire_all()
    assert clock.get_override(db) is None
    assert clock.today(db) == date.today()


def test_invalid_override_rejected(client, db):
    csrf = admin_login(client)
    resp = client.post(
        "/admin/test-date",
        data={"test_date": "kein-datum", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert "Ung%C3%BCltiges" in resp.headers["location"]
    assert clock.get_override(db) is None


def test_override_enables_future_settlement(client, db, seed):
    """Kernanwendungsfall: Abrechnung zukünftiger Termine testen."""
    event = seed["event"]  # liegt 3 Tage in der Zukunft
    db.add(Booking(event_id=event.id, member_id=seed["member"].id))
    db.commit()
    csrf = admin_login(client)

    # Ohne Override: Abrechnung verweigert
    resp = client.post(
        f"/admin/event/{event.id}/settle",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "Zukunft" in resp.headers["location"]

    # Datum auf den Termintag stellen → Abrechnung läuft
    client.post(
        "/admin/test-date",
        data={"test_date": event.date.isoformat(), "csrf_token": csrf},
    )
    resp = client.post(
        f"/admin/event/{event.id}/settle",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "abgerechnet" in resp.headers["location"]
    assert db.query(Payment).filter(Payment.type == Payment.TYPE_CHARGE).count() == 1
