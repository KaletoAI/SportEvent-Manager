"""Eingefrorene Abrechnung, Link-Gäste in Zahlungsaufforderungen,
Schutz vor doppeltem Versand und PayPal-Link der Mitglieder."""

from decimal import Decimal

import pytest
from conftest import admin_login, member_login

from app import services
from app.models.models import Booking, Event, GuestBooking, Member, Subscription
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
def guest_setup(db, seed):
    """Vergangener Termin: Anna + Link-Gast Gina (2 Personen) → 3 Teilnehmer,
    Mitglied 8/3 = 2,67 €, Gast 10/3 = 3,33 € pro Person."""
    event = seed["past_event"]
    db.add(Booking(event_id=event.id, member_id=seed["member"].id))
    gb = GuestBooking(event_id=event.id, name="Gina", email="Gina@Example.com", count=2)
    db.add(gb)
    db.commit()
    seed["gb"] = gb
    return seed


def _settle(client, event_id):
    csrf = admin_login(client)
    client.post(f"/admin/event/{event_id}/settle", data={"csrf_token": csrf})
    return csrf


# ── Eingefrorene Abrechnung / Fehlbuchung entfernen ────────────────────────


def test_settlement_freezes_shares(client, db, guest_setup):
    event = guest_setup["past_event"]
    _settle(client, event.id)
    db.expire_all()
    event = db.get(Event, event.id)
    assert event.settled_participants == 3
    assert event.settled_member_share == Decimal("2.67")
    assert event.settled_guest_share == Decimal("3.33")


def test_admin_removes_guest_after_settlement_result_stays(client, db, guest_setup):
    event = guest_setup["past_event"]
    csrf = _settle(client, event.id)
    db.expire_all()
    credit = db.get(Member, guest_setup["member"].id).credit
    resp = client.post(
        f"/admin/guest-booking/{guest_setup['gb'].id}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "mt=success" in resp.headers["location"]
    db.expire_all()
    assert db.query(GuestBooking).count() == 0
    assert db.get(Member, guest_setup["member"].id).credit == credit
    shares = services.event_shares(db, db.get(Event, event.id))
    assert shares["participants"] == 3
    assert shares["member_share"] == Decimal("2.67")
    resp = client.get(f"/admin/event/{event.id}")
    assert "Abgerechnet mit <strong>3</strong> Teilnehmern" in resp.text


def test_paid_guest_cannot_be_removed_after_settlement(client, db, guest_setup):
    csrf = _settle(client, guest_setup["past_event"].id)
    services.mark_guest_paid(db, guest_setup["gb"], Decimal("6.66"))
    resp = client.post(
        f"/admin/guest-booking/{guest_setup['gb'].id}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "mt=error" in resp.headers["location"]
    assert db.query(GuestBooking).count() == 1


def test_schema_upgrade_freezes_already_settled_events(db, guest_setup):
    event = guest_setup["past_event"]
    services.settle_event(db, event)
    engine = db.bind
    with engine.begin() as conn:
        for col in ("settled_participants", "settled_member_share", "settled_guest_share"):
            conn.exec_driver_sql(f"ALTER TABLE events DROP COLUMN {col}")
    upgrade(engine)
    upgrade(engine)
    db.expire_all()
    event = db.get(Event, event.id)
    assert event.settled_participants == 3
    assert event.settled_guest_share == Decimal("3.33")
    assert db.get(Event, guest_setup["event"].id).settled_participants is None


# ── Link-Gäste im manuellen Modus und in der Sammelmail ────────────────────


def test_manual_mode_sends_no_guest_mail_on_settlement(client, db, guest_setup, outbox):
    guest_setup["sub"].payment_mail_mode = "manual"
    db.commit()
    _settle(client, guest_setup["past_event"].id)
    assert outbox == []


def test_auto_mode_still_mails_guest_on_settlement(client, db, guest_setup, outbox):
    _settle(client, guest_setup["past_event"].id)
    assert sorted(m["to"] for m in outbox) == ["Gina@Example.com", "anna@example.com"]


def test_payment_requests_include_open_guests(client, db, guest_setup, outbox):
    sub = guest_setup["sub"]
    sub.payment_mail_mode = "manual"
    db.commit()
    csrf = _settle(client, guest_setup["past_event"].id)
    debts = services.guest_debts(db, sub)
    assert [(g["name"], g["email"], g["debt"]) for g in debts] == [
        ("Gina", "gina@example.com", Decimal("6.66"))
    ]
    resp = client.get(f"/admin/subscription/{sub.id}")
    assert "Offene Gastzahlungen" in resp.text
    client.post(
        f"/admin/subscription/{sub.id}/payment-mails/send",
        data={"csrf_token": csrf},
    )
    guest_mail = next(m for m in outbox if m["to"] == "gina@example.com")
    date = guest_setup["past_event"].date.strftime("%d.%m.%Y")
    assert f"Gast {date} (2 Personen): 6,66 €" in guest_mail["body"]
    assert "Bitte zahle 6,66 € per PayPal" in guest_mail["body"]
    assert "pay@example.com" in guest_mail["body"]


def test_paid_or_unsettled_guests_get_no_request(db, guest_setup):
    sub = guest_setup["sub"]
    assert services.guest_debts(db, sub) == []  # noch nicht abgerechnet
    services.settle_event(db, guest_setup["past_event"])
    services.mark_guest_paid(db, guest_setup["gb"], Decimal("6.66"))
    assert services.guest_debts(db, sub) == []


# ── Doppelter Versand ──────────────────────────────────────────────────────


def test_second_click_on_stale_page_sends_nothing(client, db, seed, outbox):
    sub = seed["sub"]
    seed["member"].credit = Decimal("-4.00")
    db.commit()
    csrf = admin_login(client)
    url = f"/admin/subscription/{sub.id}/payment-mails/send"
    client.post(url, data={"csrf_token": csrf, "last_sent": ""})
    assert len(outbox) == 1
    # Zweiter Tipp mit dem Stand der alten Seite
    resp = client.post(
        url, data={"csrf_token": csrf, "last_sent": ""}, follow_redirects=False
    )
    assert "mt=error" in resp.headers["location"]
    assert len(outbox) == 1

    # Die neu geladene Seite fragt ausdrücklich nach …
    db.expire_all()
    sub = db.get(Subscription, sub.id)
    assert services.payment_request_is_repeat(db, sub)
    page = client.get(f"/admin/subscription/{sub.id}").text
    assert "bereits" in page and "Wirklich nochmal" in page
    assert "erneut senden" in page
    # … und mit ihrem Stand geht der bewusste zweite Versand raus
    client.post(
        url,
        data={"csrf_token": csrf, "last_sent": services.payment_request_token(sub)},
    )
    assert len(outbox) == 2


def test_new_settlement_ends_repeat_warning(db, guest_setup):
    sub = guest_setup["sub"]
    sub.last_payment_request_at = services.utcnow()
    db.commit()
    assert services.payment_request_is_repeat(db, sub)
    services.settle_event(db, guest_setup["past_event"])
    assert not services.payment_request_is_repeat(db, sub)


# ── PayPal-Link ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", ""),
        ("  paypal.me/Sina ", "https://paypal.me/Sina"),
        ("https://www.paypal.me/sina", "https://paypal.me/sina"),
        ("paypal.com/paypalme/sina", "https://www.paypal.com/paypalme/sina"),
        ("http://PayPal.Me/sina", "https://paypal.me/sina"),
        ("https://www.paypal.com/paypalme/sina", "https://www.paypal.com/paypalme/sina"),
        ("https://evil.example/paypal.me/sina", None),
        ("https://paypal.me.evil.example/sina", None),
        ("javascript:alert(1)", None),
        ("https://paypal.me/", None),
    ],
)
def test_normalize_paypal_link(raw, expected):
    assert services.normalize_paypal_link(raw) == expected


def test_paypal_pay_url_prefills_amount():
    assert (
        services.paypal_pay_url("https://paypal.me/sina", Decimal("6.666"))
        == "https://paypal.me/sina/6.67EUR"
    )
    assert (
        services.paypal_pay_url("https://www.paypal.com/paypalme/sina", Decimal("5"))
        == "https://www.paypal.com/paypalme/sina"
    )
    assert services.paypal_pay_url("", Decimal("5")) == ""


def test_member_sets_own_paypal_link(client, db, seed):
    csrf = member_login(client, email="sina@example.com")
    resp = client.post(
        "/member/paypal-link",
        data={"paypal_link": "paypal.me/sina", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert "mt=success" in resp.headers["location"]
    db.expire_all()
    assert db.get(Member, seed["super_member"].id).paypal_link == "https://paypal.me/sina"

    resp = client.post(
        "/member/paypal-link",
        data={"paypal_link": "https://evil.example/x", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert "mt=error" in resp.headers["location"]
    db.expire_all()
    assert db.get(Member, seed["super_member"].id).paypal_link == "https://paypal.me/sina"
    assert "https://paypal.me/sina" in client.get("/member/dashboard").text


def test_payee_paypal_link_in_payment_mails(client, db, guest_setup, outbox):
    sub = guest_setup["sub"]
    sub.payout_mode = "member"
    sina = guest_setup["super_member"]
    sina.paypal_address = "sina@pay.me"
    sina.paypal_link = "https://paypal.me/sina"
    sina.credit = Decimal("100.00")
    guest_setup["member"].credit = Decimal("0.00")
    db.commit()
    _settle(client, guest_setup["past_event"].id)
    anna = next(m for m in outbox if m["to"] == "anna@example.com")
    assert "https://paypal.me/sina/2.67EUR" in anna["body"]
    assert 'href="https://paypal.me/sina/2.67EUR"' in anna["html"]
    gina = next(m for m in outbox if m["to"] == "Gina@Example.com")
    assert "https://paypal.me/sina/6.66EUR" in gina["body"]

    # Banner im Dashboard des Schuldners
    member_login(client)
    page = client.get("/member/dashboard").text
    assert 'href="https://paypal.me/sina/2.67EUR"' in page


# ── Skript: versehentliche Bezahlt-Klicks aus dem Kontoauszug entfernen ────


def test_script_removes_paid_reversal_pairs(db, guest_setup):
    """3× Bezahlt ✓, 2× Zurücknehmen → 2 Paare weg, letzte Buchung bleibt,
    Guthaben und Bezahlt-Status unverändert."""
    import importlib.util
    from pathlib import Path

    from app.models.models import Payment

    path = Path(__file__).resolve().parent.parent / "scripts/remove_guest_paid_reversals.py"
    spec = importlib.util.spec_from_file_location("cleanup", path)
    cleanup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cleanup)

    sub, gb = guest_setup["sub"], guest_setup["gb"]
    sub.payout_mode = "member"
    sina = guest_setup["super_member"]
    sina.paypal_address = "sina@pay.me"
    sina.credit = Decimal("100.00")
    db.commit()
    services.settle_event(db, guest_setup["past_event"])
    # Altbestand: früher schrieb „Zurücknehmen“ eine Storno-Buchung
    for _ in range(2):
        for amount, note in (
            ("-6.66", "Gastzahlung von Gina erhalten"),
            ("6.66", "Storno Gastzahlung von Gina"),
        ):
            db.add(
                Payment(
                    member_id=sina.id,
                    amount=Decimal(amount),
                    event_id=gb.event_id,
                    note=note,
                )
            )
    db.commit()
    services.mark_guest_paid(db, gb, Decimal("6.66"))
    db.refresh(sina)
    credit = sina.credit
    assert credit == Decimal("93.34")

    pairs, rest = cleanup.find_reversal_pairs(db, "Gina")
    assert len(pairs) == 2
    assert [p.note for p in rest] == ["Gastzahlung von Gina erhalten"]
    for received, storno in pairs:
        db.delete(received)
        db.delete(storno)
    db.commit()

    db.expire_all()
    notes = [p.note for p in db.query(Payment).filter(Payment.member_id == sina.id)]
    assert notes == ["Gastzahlung von Gina erhalten"]
    assert db.get(Member, sina.id).credit == credit
    assert db.get(GuestBooking, gb.id).paid_at is not None
    assert cleanup.find_reversal_pairs(db, "Gina")[0] == []
