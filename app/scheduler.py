"""Background jobs: cancellation reminders, automatic settlement and
housekeeping (expired sessions / login tokens).

Runs as an asyncio task (see app.main lifespan) every
`settings.scheduler_interval_seconds`. All date logic uses clock.today(db)
so the admin's test-date override drives these jobs too — settle flows can
be tested end-to-end by moving the date forward.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from app import clock, services
from app.auth import purge_expired
from app.config import settings
from app.database import SessionLocal
from app.emailer import (
    Mail,
    cancel_reminder_email_body,
    guest_reminder_email_body,
    send_batch,
)
from app.templates import format_date
from app.models.models import Event, GuestBooking

logger = logging.getLogger(__name__)


async def send_cancel_reminders(db) -> int:
    """Mail booked members ~24h before the last free cancellation moment.

    Last free moment = event start - cancel_hours_free; the reminder goes
    out once `now` is within 24h of it. Idempotent via Event.reminder_sent."""
    now = clock.now(db)
    sent_total = 0
    events = (
        db.query(Event)
        .filter(
            Event.is_cancelled == False,  # noqa: E712
            Event.settled_at.is_(None),
            Event.reminder_sent == False,  # noqa: E712
            Event.date >= now.date(),
        )
        .all()
    )
    for event in events:
        sub = event.subscription
        start = datetime.combine(event.date, event.start_time)
        last_free = start - timedelta(hours=sub.cancel_hours_free)
        if not (timedelta(0) < last_free - now <= timedelta(hours=24)):
            continue
        mails = [
            Mail(
                booking.member.email,
                f"Erinnerung: Abmeldefrist {format_date(event.date)} – {sub.name}",
                cancel_reminder_email_body(
                    booking.member.name,
                    event.date.strftime("%d.%m.%Y"),
                    last_free.strftime("%d.%m.%Y %H:%M"),
                ),
            )
            for booking in event.bookings
        ]
        # Link-Gäste erinnern (können nicht selbst stornieren → Bitte,
        # dem Organisator abzusagen, damit der Platz frei wird)
        guest_bookings = (
            db.query(GuestBooking)
            .filter(GuestBooking.event_id == event.id)
            .all()
        )
        mails += [
            Mail(
                gb.email,
                f"Erinnerung: Termin {format_date(event.date)} – {sub.name}",
                guest_reminder_email_body(
                    gb.name,
                    event.date.strftime("%d.%m.%Y"),
                    event.start_time.strftime("%H:%M"),
                    gb.count,
                ),
            )
            for gb in guest_bookings
            if gb.email
        ]
        sent = await send_batch(sub, mails)
        event.reminder_sent = True
        db.commit()
        sent_total += sent
        logger.info(
            "Reminder for event %s (%s): %d mails", event.id, event.date, sent
        )
    return sent_total


async def auto_settle_events(db) -> int:
    """Settle events whose date has passed (from the day after onwards).

    Skips events with a blocker (e.g. below minimum participants) — those
    stay 'Offen' for the admin/super member to resolve manually."""
    today = clock.today(db)
    settled = 0
    events = (
        db.query(Event)
        .filter(
            Event.is_cancelled == False,  # noqa: E712
            Event.settled_at.is_(None),
            Event.date < today,
        )
        .all()
    )
    for event in events:
        if services.settle_blocker(db, event):
            continue
        charged, total, sent = await services.settle_and_notify(db, event)
        settled += 1
        logger.info(
            "Auto-settled event %s (%s): %d bookings, %s €, %d mails",
            event.id, event.date, charged, total, sent,
        )
    return settled


async def run_jobs() -> None:
    db = SessionLocal()
    try:
        await send_cancel_reminders(db)
        await auto_settle_events(db)
        purge_expired(db)
    except Exception:
        logger.exception("Scheduler run failed")
    finally:
        db.close()


async def scheduler_loop() -> None:
    logger.info(
        "Scheduler started (interval %ss)", settings.scheduler_interval_seconds
    )
    while True:
        await run_jobs()
        await asyncio.sleep(settings.scheduler_interval_seconds)
