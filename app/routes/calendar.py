"""Persönlicher Kalender-Feed: /kalender/{Person.calendar_token}.ics

Kalender-Apps (Proton, Google, Apple, Outlook, Thunderbird …) abonnieren
diese URL und holen sie regelmäßig neu — ohne Login, der Token ist das
Geheimnis (neu erzeugbar im Tab „Meine“). Der Feed enthält die Anmeldungen
aller aktiven Mitgliedschaften derselben Person, abgesagte Termine mit
STATUS:CANCELLED, damit sie aus dem Kalender verschwinden bzw. markiert werden.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app import clock, ical, services
from app.database import get_db
from app.models.models import Booking, Event, Member, Person
from app.templates import format_date, format_time
from app.web import public_base_url

router = APIRouter()

# Vergangene Termine bleiben so lange im Kalender sichtbar
PAST_DAYS = 90


def _summary(b: Booking) -> str:
    text = b.event.subscription.name
    if b.event.is_extra:
        text += " (Zusatz)"
    if b.guest_count:
        text += f" (+{b.guest_count} {'Gast' if b.guest_count == 1 else 'Gäste'})"
    if b.event.is_cancelled:
        text = "Abgesagt: " + text
    elif b.cancel_requested_at:
        text += " – Abmeldung angefragt"
    return text


def _description(b: Booking, start: datetime) -> str:
    sub = b.event.subscription
    lines = []
    if sub.description:
        lines.append(sub.description.strip())
    code = services.door_code(b.event)
    if code and not b.event.is_cancelled:
        lines.append(f"Türcode: {code}")
    if not b.event.is_cancelled and not b.event.settled_at:
        free_until = start - timedelta(hours=sub.cancel_hours_free)
        lines.append(
            f"Kostenlos abmelden bis {format_date(free_until)}, "
            f"{format_time(free_until.time())} Uhr."
        )
    return "\n\n".join(lines)


@router.get("/{token}.ics")
async def calendar_feed(token: str, request: Request, db: Session = Depends(get_db)):
    person = (
        db.query(Person).filter(Person.calendar_token == token).first()
        if token
        else None
    )
    if person is None:
        raise HTTPException(status_code=404, detail="Kalender nicht gefunden")

    since = clock.today(db) - timedelta(days=PAST_DAYS)
    bookings = (
        db.query(Booking)
        .join(Event)
        .join(Member, Booking.member_id == Member.id)
        .filter(
            Member.email == person.email,
            Member.is_active == True,  # noqa: E712
            Event.date >= since,
        )
        .order_by(Event.date, Event.start_time)
        .all()
    )
    dashboard = f"{public_base_url(request)}member/dashboard"
    events = []
    for b in bookings:
        start = datetime.combine(b.event.date, b.event.start_time)
        end = datetime.combine(b.event.date, b.event.end_time)
        if end <= start:  # Termin über Mitternacht
            end += timedelta(days=1)
        events.append(
            ical.CalEvent(
                uid=f"event-{b.event.id}@sportabo",
                start=start,
                end=end,
                summary=_summary(b),
                description=_description(b, start),
                url=dashboard,
                cancelled=b.event.is_cancelled,
            )
        )
    return Response(
        content=ical.build_calendar("SportAbo", events),
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'inline; filename="sportabo.ics"',
            "Cache-Control": "private, max-age=900",
            "X-Robots-Tag": "noindex",
        },
    )
