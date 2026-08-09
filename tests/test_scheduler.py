"""Scheduler-Jobs: Storno-Erinnerung und Auto-Abrechnung."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app import clock, scheduler
from app.models.models import Booking, Event, Member, Payment


@pytest.mark.anyio
async def test_auto_settle_after_event(db, seed):
    """Nach Ablauf des Termindatums wird automatisch abgerechnet."""
    event = seed["past_event"]
    db.add(Booking(event_id=event.id, member_id=seed["member"].id))
    db.commit()

    settled = await scheduler.auto_settle_events(db)
    assert settled == 1
    db.expire_all()
    assert db.get(Event, event.id).settled_at is not None
    member = db.query(Member).filter(Member.id == seed["member"].id).one()
    assert member.credit == Decimal("12.00")  # 20 − 8 (allein am Termin)

    # Idempotent: zweiter Lauf rechnet nichts mehr ab
    assert await scheduler.auto_settle_events(db) == 0


@pytest.mark.anyio
async def test_auto_settle_skips_below_minimum(db, seed):
    event = seed["past_event"]
    event.min_participants = 3
    db.add(Booking(event_id=event.id, member_id=seed["member"].id))
    db.commit()

    settled = await scheduler.auto_settle_events(db)
    assert settled == 0
    db.expire_all()
    assert db.get(Event, event.id).settled_at is None
    assert db.query(Payment).count() == 0


@pytest.mark.anyio
async def test_reminder_flag_set_on_deadline(db, seed):
    """Erinnerung ~24h vor dem letzten kostenlosen Abmeldezeitpunkt."""
    from datetime import datetime

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
    db.commit()

    await scheduler.send_cancel_reminders(db)
    db.expire_all()
    assert db.get(Event, event.id).reminder_sent is True
    # Anderes Event (falscher Tag) bleibt unberührt
    assert db.get(Event, seed["event"].id).reminder_sent is False


@pytest.mark.anyio
async def test_scheduler_respects_date_override(client, db, seed):
    """Test-Datum treibt auch die Auto-Abrechnung (Kern des Debug-Flows)."""
    from conftest import admin_login

    future = seed["event"]  # +3 Tage
    db.add(Booking(event_id=future.id, member_id=seed["member"].id))
    db.commit()

    assert await scheduler.auto_settle_events(db) == 0  # noch nicht fällig

    csrf = admin_login(client)
    client.post(
        "/admin/test-date",
        data={
            "test_date": (future.date + timedelta(days=1)).isoformat(),
            "csrf_token": csrf,
        },
    )
    db.expire_all()
    assert await scheduler.auto_settle_events(db) == 1
