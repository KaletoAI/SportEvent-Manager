"""Offene Beträge („Wer schuldet mir wie viel“) und Zahlungsmails
automatisch vs. manuell per Knopf."""

from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import admin_login, member_login

from app import services
from app.models.models import Booking, Member, Payment, Subscription, utcnow
from app.schema_upgrade import upgrade


@pytest.fixture
def outbox(monkeypatch):
    mails = []

    async def fake_send_batch(subscription, batch):
        mails.extend(
            {"to": m.to, "subject": m.subject, "body": m.body, "html": m.html}
            for m in batch
        )
        return len(batch)

    monkeypatch.setattr(services, "send_batch", fake_send_batch)
    return mails


@pytest.fixture
def payee_setup(db, seed):
    """Vorstreck-Modell: Sina ist Zahlungsempfängerin, Anna schuldet.
    Anna: −10 (01.10.) −10 (08.10.) +5 Zahlung → −15 offen."""
    sub = seed["sub"]
    sub.payout_mode = "member"
    sina, anna = seed["super_member"], seed["member"]
    sina.paypal_address = "sina@pay.me"
    sina.credit = Decimal("100.00")
    anna.credit = Decimal("0.00")
    start = utcnow() - timedelta(days=10)
    for i, (amount, note) in enumerate(
        [
            ("-10.00", "Teilnahme 01.10.2026"),
            ("-10.00", "Teilnahme 08.10.2026"),
            ("5.00", "Zahlung an Sina"),
        ]
    ):
        db.add(
            Payment(
                member_id=anna.id,
                amount=Decimal(amount),
                type=Payment.TYPE_CHARGE if amount.startswith("-") else Payment.TYPE_DEPOSIT,
                note=note,
                created_at=start + timedelta(days=i),
            )
        )
        anna.credit += Decimal(amount)
    db.commit()
    return seed


def test_open_items_oldest_charges_are_paid_first(db, payee_setup):
    items = services.open_items(db, payee_setup["member"])
    assert [(i["note"], i["amount"], i["partial"]) for i in items] == [
        ("Teilnahme 01.10.2026", Decimal("5.00"), True),
        ("Teilnahme 08.10.2026", Decimal("10.00"), False),
    ]


def test_open_items_covers_balance_without_ledger(db, seed):
    seed["member"].credit = Decimal("-7.50")
    db.commit()
    items = services.open_items(db, seed["member"])
    assert items == [{"note": "Übertrag", "amount": Decimal("7.50"), "partial": False}]
    seed["member"].credit = Decimal("3.00")
    assert services.open_items(db, seed["member"]) == []


def test_debtors_excludes_payee_and_members_in_plus(db, payee_setup):
    # Auch die Empfängerin im Minus taucht nicht als Schuldnerin auf
    payee_setup["super_member"].credit = Decimal("-1.00")
    db.commit()
    debts = services.debtors(db, payee_setup["sub"])
    assert [(d["member"].name, d["debt"]) for d in debts] == [
        ("Anna", Decimal("15.00"))
    ]


def test_payee_sees_who_owes_how_much(client, db, payee_setup):
    member_login(client, email="sina@example.com")
    resp = client.get("/member/dashboard")
    assert "Wer schuldet mir wie viel" in resp.text
    assert "Teilnahme 08.10.2026" in resp.text
    assert "15,00 €" in resp.text
    assert 'name="payer_id" value="%s"' % payee_setup["member"].id in resp.text


def test_non_payee_sees_no_debt_overview(client, db, payee_setup):
    member_login(client)  # Anna
    resp = client.get("/member/dashboard")
    assert "Wer schuldet mir wie viel" not in resp.text


def test_payee_confirms_payment_from_overview(client, db, payee_setup):
    csrf = member_login(client, email="sina@example.com")
    client.post(
        "/member/transfer-received",
        data={
            "payer_id": payee_setup["member"].id,
            "amount": "15.00",
            "csrf_token": csrf,
        },
    )
    db.expire_all()
    assert db.get(Member, payee_setup["member"].id).credit == Decimal("0.00")
    assert services.debtors(db, payee_setup["sub"]) == []


def test_only_payee_may_change_mail_mode(client, db, payee_setup):
    sub_id = payee_setup["sub"].id
    csrf = member_login(client)  # Anna ist nicht Empfängerin
    resp = client.post(
        "/member/payment-mails/mode",
        data={"mode": "manual", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert "mt=error" in resp.headers["location"]
    db.expire_all()
    assert db.get(Subscription, sub_id).payment_mail_mode == "auto"


def test_payee_switches_mail_mode(client, db, payee_setup):
    sub_id = payee_setup["sub"].id
    csrf = member_login(client, email="sina@example.com")
    client.post(
        "/member/payment-mails/mode", data={"mode": "manual", "csrf_token": csrf}
    )
    db.expire_all()
    assert db.get(Subscription, sub_id).payment_mail_mode == "manual"
    client.post(
        "/member/payment-mails/mode", data={"mode": "bogus", "csrf_token": csrf}
    )
    db.expire_all()
    assert db.get(Subscription, sub_id).payment_mail_mode == "manual"


def _settle_with_anna(client, db, seed):
    event = seed["past_event"]
    db.add(Booking(event_id=event.id, member_id=seed["member"].id))
    db.commit()
    csrf = admin_login(client)
    client.post(f"/admin/event/{event.id}/settle", data={"csrf_token": csrf})


def test_auto_mode_mails_on_settlement(client, db, payee_setup, outbox):
    _settle_with_anna(client, db, payee_setup)
    assert [m["to"] for m in outbox] == ["anna@example.com"]


def test_manual_mode_skips_member_mails_on_settlement(
    client, db, payee_setup, outbox
):
    payee_setup["sub"].payment_mail_mode = "manual"
    db.commit()
    _settle_with_anna(client, db, payee_setup)
    assert outbox == []
    db.expire_all()
    # Belastet wird trotzdem
    assert db.get(Member, payee_setup["member"].id).credit == Decimal("-23.00")


def test_payee_sends_collected_payment_requests(client, db, payee_setup, outbox):
    sub_id = payee_setup["sub"].id
    csrf = member_login(client, email="sina@example.com")
    resp = client.post(
        "/member/payment-mails/send",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "1%20Zahlungsaufforderung%20verschickt" in resp.headers["location"]
    assert len(outbox) == 1
    mail = outbox[0]
    assert mail["to"] == "anna@example.com"
    assert "15,00 €" in mail["body"]
    assert "Teilnahme 01.10.2026 (Rest): 5,00 €" in mail["body"]
    assert "Teilnahme 08.10.2026: 10,00 €" in mail["body"]
    assert "an Sina per PayPal" in mail["body"]
    assert "sina@pay.me" in mail["body"]
    db.expire_all()
    assert db.get(Subscription, sub_id).last_payment_request_at is not None


def test_non_payee_cannot_send_payment_requests(client, db, payee_setup, outbox):
    csrf = member_login(client)
    resp = client.post(
        "/member/payment-mails/send",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "mt=error" in resp.headers["location"]
    assert outbox == []


def test_send_without_smtp_reports_error(client, db, payee_setup):
    csrf = member_login(client, email="sina@example.com")
    resp = client.post(
        "/member/payment-mails/send",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "mt=error" in resp.headers["location"]
    db.expire_all()
    assert db.get(Subscription, payee_setup["sub"].id).last_payment_request_at is None


def test_admin_sees_debts_and_sends_requests(client, db, payee_setup, outbox):
    sub = payee_setup["sub"]
    csrf = admin_login(client)
    resp = client.get(f"/admin/subscription/{sub.id}")
    assert "Offene Beträge" in resp.text
    assert "Teilnahme 08.10.2026" in resp.text

    resp = client.post(
        f"/admin/subscription/{sub.id}/payment-mails/send",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.headers["location"].startswith(f"/admin/subscription/{sub.id}")
    assert [m["to"] for m in outbox] == ["anna@example.com"]


def test_central_mode_requests_name_central_address(client, db, seed, outbox):
    seed["member"].credit = Decimal("-4.00")
    db.commit()
    csrf = admin_login(client)
    client.post(
        f"/admin/subscription/{seed['sub'].id}/payment-mails/send",
        data={"csrf_token": csrf},
    )
    assert len(outbox) == 1
    assert "pay@example.com" in outbox[0]["body"]


def test_schema_upgrade_adds_payment_mail_columns(db, seed):
    engine = db.bind
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE subscriptions DROP COLUMN payment_mail_mode")
        conn.exec_driver_sql(
            "ALTER TABLE subscriptions DROP COLUMN last_payment_request_at"
        )
    upgrade(engine)
    upgrade(engine)
    db.expire_all()
    sub = db.get(Subscription, seed["sub"].id)
    assert sub.payment_mail_mode == "auto"
    assert sub.last_payment_request_at is None


def _open_block(body: str) -> str:
    """Abschnitt „Offene Termine“ … „Offen gesamt“ aus dem Klartext."""
    start = body.index("Offene Termine")
    end = body.index("\n", body.index("Offen gesamt"))
    return body[start:end]


def test_settlement_mail_lists_open_items_like_payment_request(
    client, db, payee_setup, outbox
):
    """Im Minus enthält die automatische Abrechnungsmail dieselben offenen
    Termine und dieselbe Zahlungsbox wie die manuelle Sammelmail."""
    _settle_with_anna(client, db, payee_setup)
    settlement = outbox[0]["body"]
    date = payee_setup["past_event"].date.strftime("%d.%m.%Y")
    assert "Dein Betrag: 8,00 €" in settlement
    assert "Teilnahme 01.10.2026 (Rest): 5,00 €" in settlement
    assert "Teilnahme 08.10.2026: 10,00 €" in settlement
    assert f"Teilnahme {date}: 8,00 €" in settlement
    assert "Offen gesamt: 23,00 €" in settlement
    assert "Bitte zahle 23,00 € an Sina per PayPal" in settlement

    outbox.clear()
    csrf = member_login(client, email="sina@example.com")
    client.post("/member/payment-mails/send", data={"csrf_token": csrf})
    request = outbox[0]["body"]
    assert _open_block(request) == _open_block(settlement)
    assert "Bitte zahle 23,00 € an Sina per PayPal" in request


def test_settlement_mail_without_debt_needs_no_payment(client, db, payee_setup, outbox):
    payee_setup["member"].credit = Decimal("50.00")
    db.commit()
    _settle_with_anna(client, db, payee_setup)
    body = outbox[0]["body"]
    assert "du musst nichts zahlen" in body
    assert "Offene Termine" not in body
    assert "Bitte zahle" not in body
