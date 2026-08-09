"""Abrechnungs- und Erinnerungsmails gehen auch an Link-Gäste."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app import scheduler, services
from app.models.models import Booking, Event, GuestBooking


@pytest.fixture
def outbox(monkeypatch):
    """Fängt alle Mails ab (Settlement + Scheduler)."""
    mails = []

    async def fake_send_email(subscription, to, subject, body, html_body=None):
        mails.append({"to": to, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(services, "send_email", fake_send_email)
    monkeypatch.setattr(scheduler, "send_email", fake_send_email)
    return mails


@pytest.mark.anyio
async def test_settlement_mails_member_and_guest(db, seed, outbox):
    event = seed["past_event"]
    db.add(Booking(event_id=event.id, member_id=seed["member"].id))
    db.add(
        GuestBooking(
            event_id=event.id, name="Gustav", email="gustav@example.com", count=1
        )
    )
    db.commit()

    charged, total, sent = await services.settle_and_notify(db, event)
    assert charged == 1
    assert sent == 2  # Mitglied + Gast

    guest_mail = next(m for m in outbox if m["to"] == "gustav@example.com")
    assert "Abrechnung" in guest_mail["subject"]
    # 2 Teilnehmer → Gast-Anteil 10/2 = 5,00 €, zentrale PayPal-Adresse
    assert "5,00" in guest_mail["body"]
    assert "pay@example.com" in guest_mail["body"]


@pytest.mark.anyio
async def test_settlement_skips_paid_and_emailless_guests(db, seed, outbox):
    event = seed["past_event"]
    db.add(Booking(event_id=event.id, member_id=seed["member"].id))
    db.add(
        GuestBooking(
            event_id=event.id,
            name="Paula",
            email="paula@example.com",
            count=1,
            paid_at=datetime.now(),
            paid_amount=Decimal("5.00"),
        )
    )
    db.add(GuestBooking(event_id=event.id, name="Ohne Mail", email="", count=1))
    db.commit()

    await services.settle_and_notify(db, event)
    recipients = [m["to"] for m in outbox]
    assert "paula@example.com" not in recipients
    assert recipients == [seed["member"].email]


@pytest.mark.anyio
async def test_reminder_mails_guest(db, seed, outbox):
    sub = seed["sub"]
    # Start in ~48h, Frist 36h → letzter freier Zeitpunkt in ~12h → fällig
    sub.cancel_hours_free = 36
    event = Event(
        subscription_id=sub.id,
        date=date.today() + timedelta(days=2),
        start_time=datetime.now().time().replace(microsecond=0),
        end_time=seed["event"].end_time,
        max_participants=8,
        min_participants=1,
    )
    db.add(event)
    db.flush()
    db.add(Booking(event_id=event.id, member_id=seed["member"].id))
    db.add(
        GuestBooking(
            event_id=event.id, name="Gustav", email="gustav@example.com", count=2
        )
    )
    db.commit()

    sent = await scheduler.send_cancel_reminders(db)
    assert sent == 2  # Mitglied + Gast

    guest_mail = next(m for m in outbox if m["to"] == "gustav@example.com")
    assert "Erinnerung" in guest_mail["subject"]
    assert "als Gast" in guest_mail["body"]
    assert "mit 2 Personen" in guest_mail["body"]
    # Idempotent: zweiter Lauf schickt nichts mehr
    assert await scheduler.send_cancel_reminders(db) == 0
