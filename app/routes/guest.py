"""Guest routes: public booking via shared link (no auth)."""

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from app import clock, services
from app.auth import check_rate_limit
from app.database import get_db
from app.models.models import Event, GuestBooking, Subscription
from app.templates import TemplateResponse
from app.web import client_ip, flash_redirect

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Guest bookings per IP and hour — the link is public once shared
MAX_GUEST_BOOKINGS_PER_HOUR = 20


def _get_event(db: Session, token: str) -> Event:
    event = db.query(Event).filter(Event.public_token == token).first()
    if not event:
        raise HTTPException(status_code=404, detail="Termin nicht gefunden")
    return event


def _event_page(request: Request, db: Session, event: Event, msg: str, mt: str):
    if event.is_cancelled:
        return TemplateResponse(
            "guest/event.html",
            {"request": request, "event": event, "cancelled": True, "msg": msg,
             "msg_type": mt},
        )

    total_booked = services.count_booked(db, event.id)
    available = max(0, event.max_participants - total_booked)

    return TemplateResponse(
        "guest/event.html",
        {
            "request": request,
            "event": event,
            "subscription": event.subscription,
            "total_booked": total_booked,
            "available": available,
            # the next booking would go beyond the minimum → request
            "needs_approval": total_booked >= event.min_participants,
            "max_share": services.guest_max_share(event),
            "cancelled": False,
            "expired": bool(event.settled_at) or event.date < clock.today(db),
            "msg": msg,
            "msg_type": mt,
        },
    )


@router.get("/abo/{guest_token}")
async def guest_abo_page(
    request: Request,
    guest_token: str,
    msg: str = "",
    mt: str = "success",
    db: Session = Depends(get_db),
):
    """Abo-wide guest link: shows the next event, bookable from
    `guest_days_ahead` days before its date."""
    sub = (
        db.query(Subscription)
        .filter(Subscription.guest_token == guest_token)
        .first()
    )
    if not sub:
        raise HTTPException(status_code=404, detail="Link nicht gefunden")
    today = clock.today(db)
    event = services.next_guest_event(db, sub, today)
    if event and services.guest_bookable_from(sub, event) <= today:
        return _event_page(request, db, event, msg, mt)
    return TemplateResponse(
        "guest/abo.html",
        {
            "request": request,
            "subscription": sub,
            "event": event,
            "bookable_from": (
                services.guest_bookable_from(sub, event) if event else None
            ),
            "msg": msg,
            "msg_type": mt,
        },
    )


@router.get("/{token}")
async def guest_event_page(
    request: Request,
    token: str,
    msg: str = "",
    mt: str = "success",
    db: Session = Depends(get_db),
):
    """Public guest booking page for an event."""
    return _event_page(request, db, _get_event(db, token), msg, mt)


@router.post("/{token}/book")
async def guest_booking(
    request: Request,
    token: str,
    name: str = Form(""),
    email: str = Form(""),
    count: int = Form(1, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """Submit a guest booking, then redirect to its confirmation page
    (Post/Redirect/Get: reloading the confirmation can't book twice)."""
    event = _get_event(db, token)

    def back(msg: str):
        return flash_redirect(msg, f"/g/{token}", mt="error")

    check_rate_limit(
        f"guest-book:{client_ip(request)}",
        MAX_GUEST_BOOKINGS_PER_HOUR,
        3600,
        "Zu viele Buchungen. Bitte später erneut versuchen.",
    )
    name = name.strip()
    email = services.normalize_email(email)
    if not name or len(name) > 100:
        return back("Bitte gib deinen Namen an (höchstens 100 Zeichen)")
    if len(email) > 200 or not _EMAIL_RE.match(email):
        return back("Bitte gib eine gültige E-Mail-Adresse an")
    if event.is_cancelled:
        return back("Termin ist abgesagt")
    if event.settled_at or event.date < clock.today(db):
        return back("Termin liegt in der Vergangenheit")

    gb = GuestBooking(event_id=event.id, email=email, name=name, count=count)
    db.add(gb)
    db.flush()
    # Capacity check AFTER insert (inside the transaction), see member booking.
    if services.count_booked(db, event.id) > event.max_participants:
        db.rollback()
        return back("Termin ist ausgebucht")
    # Beyond the minimum a super member has to confirm (spot stays reserved)
    pending = services.guest_needs_approval(db, event)
    if pending:
        gb.confirmed_at = None
    db.commit()
    if pending:
        await services.notify_guest_request(db, gb)
        return flash_redirect(
            "Anfrage gespeichert", f"/g/{token}/buchung/{gb.token}"
        )
    await services.send_guest_confirmation(db, gb)
    return flash_redirect(
        "Buchung gespeichert", f"/g/{token}/buchung/{gb.token}"
    )


@router.get("/{token}/buchung/{booking_token}")
async def guest_confirmation(
    request: Request,
    token: str,
    booking_token: str,
    db: Session = Depends(get_db),
):
    """Confirmation page of one guest booking (bookmarkable)."""
    event = _get_event(db, token)
    gb = (
        db.query(GuestBooking)
        .filter(GuestBooking.token == booking_token, GuestBooking.event_id == event.id)
        .first()
    )
    if not gb:
        raise HTTPException(status_code=404, detail="Buchung nicht gefunden")
    payee = services.payee_info(db, event.subscription)
    return TemplateResponse(
        "guest/confirmation.html",
        {
            "request": request,
            "name": gb.name,
            "email": gb.email,
            "count": gb.count,
            "confirmed": gb.confirmed_at is not None,
            # Türcode erst mit bestätigter Buchung
            "door_code": services.door_code(event) if gb.confirmed_at else "",
            "event": event,
            "subscription": event.subscription,
            "total_price": services.guest_max_share(event) * gb.count,
            "paypal": payee["paypal"],
            "payee_name": payee["name"],
        },
    )
