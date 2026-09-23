"""Guest routes: public booking via shared link (no auth)."""

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from app import clock, services
from app.auth import check_rate_limit
from app.database import get_db
from app.models.models import Event, GuestBooking
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


def _max_guest_share(event: Event):
    """Displayed maximum: the event only takes place at min_participants
    or more, so a guest never pays more than the share at the minimum."""
    return services.price_tiers(event, limit=1)[0]["guest"]


@router.get("/{token}")
async def guest_event_page(
    request: Request,
    token: str,
    msg: str = "",
    mt: str = "success",
    db: Session = Depends(get_db),
):
    """Public guest booking page for an event."""
    event = _get_event(db, token)
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
            "max_share": _max_guest_share(event),
            "cancelled": False,
            "expired": bool(event.settled_at) or event.date < clock.today(db),
            "msg": msg,
            "msg_type": mt,
        },
    )


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
    db.commit()
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
            "event": event,
            "subscription": event.subscription,
            "total_price": _max_guest_share(event) * gb.count,
            "paypal": payee["paypal"],
            "payee_name": payee["name"],
        },
    )
