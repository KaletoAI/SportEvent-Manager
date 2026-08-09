"""Bezahlt-Checkbox für Gastbuchungen: Status + Gegenbuchung beim Payee."""

from decimal import Decimal

from conftest import admin_login, member_login

from app import services
from app.database import SessionLocal
from app.models.models import GuestBooking, Member, Payment


def make_guest(db, event, name="Gustav", count=1) -> GuestBooking:
    gb = GuestBooking(event_id=event.id, name=name, email="g@example.com", count=count)
    db.add(gb)
    db.commit()
    db.refresh(gb)
    return gb


def use_member_payout(db, seed):
    """Anna (credit 20) wird Zahlungsempfängerin des Vorstreck-Modells."""
    seed["sub"].payout_mode = "member"
    seed["member"].paypal_address = "anna@pay.me"
    db.commit()


def test_admin_mark_paid_central_no_ledger(client, db, seed):
    """Zentrale Kasse: Status wird gesetzt, kein Ledger-Eintrag."""
    gb = make_guest(db, seed["event"])
    csrf = admin_login(client)
    resp = client.post(
        f"/admin/guest-booking/{gb.id}/paid",
        data={"amount": "5.00", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    db.expire_all()
    gb = db.get(GuestBooking, gb.id)
    assert gb.paid_at is not None
    assert gb.paid_amount == Decimal("5.00")
    assert gb.paid_member_id is None
    assert db.query(Payment).count() == 0
    assert db.get(Member, seed["member"].id).credit == Decimal("20.00")


def test_admin_mark_paid_member_payout_counterbooks(client, db, seed):
    """Vorstreck-Modell: Guthaben des Payees sinkt um den Betrag."""
    use_member_payout(db, seed)
    gb = make_guest(db, seed["event"])
    csrf = admin_login(client)
    resp = client.post(
        f"/admin/guest-booking/{gb.id}/paid",
        data={"amount": "5.00", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    db.expire_all()
    gb = db.get(GuestBooking, gb.id)
    anna = db.get(Member, seed["member"].id)
    assert gb.paid_member_id == anna.id
    assert anna.credit == Decimal("15.00")
    p = db.query(Payment).one()
    assert p.amount == Decimal("-5.00")
    assert p.event_id == seed["event"].id
    assert "Gastzahlung von Gustav" in p.note


def test_unmark_reverses_counterbooking(client, db, seed):
    use_member_payout(db, seed)
    gb = make_guest(db, seed["event"])
    csrf = admin_login(client)
    client.post(
        f"/admin/guest-booking/{gb.id}/paid",
        data={"amount": "5.00", "csrf_token": csrf},
        follow_redirects=False,
    )
    resp = client.post(
        f"/admin/guest-booking/{gb.id}/unpaid",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    db.expire_all()
    gb = db.get(GuestBooking, gb.id)
    assert gb.paid_at is None
    assert gb.paid_amount is None
    assert gb.paid_member_id is None
    assert db.get(Member, seed["member"].id).credit == Decimal("20.00")
    # Ledger bleibt nachvollziehbar: Buchung + Storno-Gegenbuchung
    notes = [p.note for p in db.query(Payment).all()]
    assert any("Gastzahlung von Gustav erhalten" in n for n in notes)
    assert any("Storno Gastzahlung von Gustav" in n for n in notes)


def test_double_mark_rejected(client, db, seed):
    use_member_payout(db, seed)
    gb = make_guest(db, seed["event"])
    csrf = admin_login(client)
    for _ in range(2):
        client.post(
            f"/admin/guest-booking/{gb.id}/paid",
            data={"amount": "5.00", "csrf_token": csrf},
            follow_redirects=False,
        )
    db.expire_all()
    # Nur eine Gegenbuchung, Guthaben nur einmal reduziert
    assert db.query(Payment).count() == 1
    assert db.get(Member, seed["member"].id).credit == Decimal("15.00")


def test_delete_paid_guest_booking_blocked(client, db, seed):
    gb = make_guest(db, seed["event"])
    csrf = admin_login(client)
    client.post(
        f"/admin/guest-booking/{gb.id}/paid",
        data={"amount": "5.00", "csrf_token": csrf},
        follow_redirects=False,
    )
    resp = client.post(
        f"/admin/guest-booking/{gb.id}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "stornieren" in resp.headers["location"]
    db.expire_all()
    assert db.get(GuestBooking, gb.id) is not None


def test_super_member_can_mark_paid(client, db, seed):
    use_member_payout(db, seed)
    gb = make_guest(db, seed["event"])
    csrf = member_login(client, "sina@example.com")  # Super-Member
    resp = client.post(
        f"/member/guest-booking/{gb.id}/paid",
        data={"amount": "3.50", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    db.expire_all()
    gb = db.get(GuestBooking, gb.id)
    assert gb.paid_amount == Decimal("3.50")
    assert db.get(Member, seed["member"].id).credit == Decimal("16.50")


def test_regular_member_cannot_mark_paid(client, db, seed):
    gb = make_guest(db, seed["event"])
    csrf = member_login(client, "anna@example.com")
    resp = client.post(
        f"/member/guest-booking/{gb.id}/paid",
        data={"amount": "5.00", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    db.expire_all()
    assert db.get(GuestBooking, gb.id).paid_at is None


def test_super_member_foreign_subscription_rejected(client, db, seed):
    """Super-Member eines anderen Abos darf fremde Gastbuchungen nicht abhaken."""
    seed["outsider"].is_super = True
    db.commit()
    gb = make_guest(db, seed["event"])
    csrf = member_login(client, "bernd@example.com")
    resp = client.post(
        f"/member/guest-booking/{gb.id}/paid",
        data={"amount": "5.00", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "nicht%20gefunden" in resp.headers["location"]
    db.expire_all()
    assert db.get(GuestBooking, gb.id).paid_at is None


def test_admin_event_page_shows_paid_form(client, db, seed):
    gb = make_guest(db, seed["event"])
    admin_login(client)
    resp = client.get(f"/admin/event/{seed['event'].id}")
    assert resp.status_code == 200
    assert f"/admin/guest-booking/{gb.id}/paid" in resp.text
    assert "Bezahlt ✓" in resp.text


def test_participants_page_paid_form_only_for_super(client, db, seed):
    gb = make_guest(db, seed["event"])
    member_login(client, "anna@example.com")
    resp = client.get(f"/member/event/{seed['event'].id}/participants")
    assert f"/member/guest-booking/{gb.id}/paid" not in resp.text

    client.cookies.clear()
    member_login(client, "sina@example.com")
    resp = client.get(f"/member/event/{seed['event'].id}/participants")
    assert f"/member/guest-booking/{gb.id}/paid" in resp.text
