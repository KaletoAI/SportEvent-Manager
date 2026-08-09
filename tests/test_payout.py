"""Vorstreck-Modell: Zahlungsziel + Umbuchungen zwischen Mitgliedern."""

from decimal import Decimal

from conftest import admin_login

from app import services
from app.models.models import Member, Payment


def test_payee_central_by_default(db, seed):
    payee = services.payee_info(db, seed["sub"])
    assert payee["paypal"] == "pay@example.com"
    assert payee["name"] is None


def test_payee_member_with_highest_credit(db, seed):
    sub = seed["sub"]
    sub.payout_mode = "member"
    seed["member"].paypal_address = "anna@pay.me"  # credit 20
    seed["super_member"].paypal_address = "sina@pay.me"  # credit 0
    db.commit()

    payee = services.payee_info(db, sub)
    assert payee["paypal"] == "anna@pay.me"
    assert payee["name"] == "Anna"

    # Sina streckt mehr vor → wird Zahlungsziel
    seed["super_member"].credit = Decimal("500.00")
    db.commit()
    payee = services.payee_info(db, sub)
    assert payee["name"] == "Sina"


def test_payee_falls_back_to_central_without_member_paypal(db, seed):
    sub = seed["sub"]
    sub.payout_mode = "member"
    db.commit()
    # Kein Mitglied hat eine PayPal-Adresse → zentrale Adresse
    payee = services.payee_info(db, sub)
    assert payee["paypal"] == "pay@example.com"
    assert payee["name"] is None


def test_transfer_moves_credit_between_members(client, db, seed):
    """Kai zahlt 50 an Anna (Vorstreckerin): Kai +50, Anna −50."""
    csrf = admin_login(client)
    payer = seed["super_member"]  # Sina, credit 0
    payee = seed["member"]  # Anna, credit 20

    resp = client.post(
        f"/admin/member/{payer.id}/transfer",
        data={
            "payee_id": payee.id,
            "amount": "50.00",
            "note": "PayPal 04.07.",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "Zahlung%20erfasst" in resp.headers["location"]

    db.expire_all()
    assert db.get(Member, payer.id).credit == Decimal("50.00")
    assert db.get(Member, payee.id).credit == Decimal("-30.00")  # 20 − 50
    notes = [p.note for p in db.query(Payment).all()]
    assert any("Zahlung an Anna" in n for n in notes)
    assert any("Erhalten von Sina" in n for n in notes)


def test_transfer_rejects_self_and_foreign(client, db, seed):
    csrf = admin_login(client)
    member = seed["member"]
    # an sich selbst
    resp = client.post(
        f"/admin/member/{member.id}/transfer",
        data={"payee_id": member.id, "amount": "10", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert "Ung%C3%BCltiger" in resp.headers["location"]
    # an Mitglied eines anderen Abos
    resp = client.post(
        f"/admin/member/{member.id}/transfer",
        data={"payee_id": seed["outsider"].id, "amount": "10", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert "Ung%C3%BCltiger" in resp.headers["location"]
    assert db.query(Payment).count() == 0


def test_guest_confirmation_names_fronting_member(client, db, seed):
    sub = seed["sub"]
    sub.payout_mode = "member"
    seed["member"].paypal_address = "anna@pay.me"
    db.commit()

    from conftest import get_csrf

    event = seed["event"]
    csrf = get_csrf(client, f"/g/{event.public_token}")
    resp = client.post(
        f"/g/{event.public_token}/book",
        data={"name": "Gast", "email": "g@x.de", "count": "1", "csrf_token": csrf},
    )
    assert resp.status_code == 200
    assert "Anna" in resp.text
    assert "anna@pay.me" in resp.text


def test_payee_does_not_pay_himself(db, seed):
    """Mail an den Vorstrecker enthält keinen Zahlungshinweis an sich selbst."""
    from app.emailer import settlement_email_body

    body = settlement_email_body(
        "Axel", "21.10.2026", "60,71 €", "400,71 €",
        "axel@pay.me", "Axel", is_payee=True,
    )
    assert "zahle bitte" not in body
    assert "Zahlungsempfänger" in body

    body = settlement_email_body(
        "Eric", "21.10.2026", "60,71 €", "-60,71 €",
        "axel@pay.me", "Axel", is_payee=False,
    )
    assert "an Axel per PayPal" in body


def test_dashboard_banner_skipped_for_payee(client, db, seed):
    """Vorstrecker im Minus bekommt keinen 'zahle an dich selbst'-Banner."""
    from decimal import Decimal as D

    sub = seed["sub"]
    sub.payout_mode = "member"
    anna = seed["member"]
    anna.paypal_address = "anna@pay.me"
    anna.credit = D("-10.00")  # im Minus, aber trotzdem höchstes Guthaben
    seed["super_member"].credit = D("-20.00")
    db.commit()

    from conftest import member_login

    member_login(client)  # Anna
    resp = client.get("/member/dashboard")
    assert "zahle per PayPal" not in resp.text

    # Sina (nicht Empfängerin) sieht den Banner mit Annas Adresse
    client.get("/member/logout")
    member_login(client, email="sina@example.com")
    resp = client.get("/member/dashboard")
    assert "anna@pay.me" in resp.text


def test_member_confirms_received_payment(client, db, seed):
    """Axel-Fall: Empfänger bestätigt Zahlungseingang selbst im Dashboard."""
    from decimal import Decimal as D
    from conftest import member_login

    csrf = member_login(client)  # Anna (credit 20) hat Geld von Sina erhalten
    resp = client.post(
        "/member/transfer-received",
        data={
            "payer_id": seed["super_member"].id,
            "amount": "54.65",
            "note": "PayPal",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "best%C3%A4tigt" in resp.headers["location"]
    db.expire_all()
    assert db.get(Member, seed["super_member"].id).credit == D("54.65")
    assert db.get(Member, seed["member"].id).credit == D("-34.65")  # 20 − 54,65


def test_confirm_received_rejects_self_and_foreign(client, db, seed):
    from conftest import member_login

    csrf = member_login(client)
    for bad_id in (seed["member"].id, seed["outsider"].id):
        resp = client.post(
            "/member/transfer-received",
            data={"payer_id": bad_id, "amount": "10", "csrf_token": csrf},
            follow_redirects=False,
        )
        assert "Ung%C3%BCltiger" in resp.headers["location"]
    assert db.query(Payment).count() == 0
