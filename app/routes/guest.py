"""Guest routes: public booking via shared link (no auth)."""

from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import clock, services
from app.database import get_db
from app.templates import TemplateResponse
from app.models.models import Event, GuestBooking

router = APIRouter()


def _get_event(db: Session, token: str) -> Event:
    event = db.query(Event).filter(Event.public_token == token).first()
    if not event:
        raise HTTPException(status_code=404, detail="Termin nicht gefunden")
    return event


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
            "tiers": services.price_tiers(event, limit=3),
            "cancelled": False,
            "expired": event.date < clock.today(db),
            "msg": msg,
            "msg_type": mt,
        },
    )


@router.post("/{token}/book")
async def guest_booking(
    request: Request,
    token: str,
    name: str = Form(..., min_length=1),
    email: str = Form(...),
    count: int = Form(1, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """Submit a guest booking."""
    event = _get_event(db, token)

    def back(msg: str):
        return RedirectResponse(
            url=f"/g/{token}?msg={quote(msg)}&mt=error", status_code=302
        )

    if event.is_cancelled:
        return back("Termin ist abgesagt")
    if event.date < clock.today(db):
        return back("Termin liegt in der Vergangenheit")

    gb = GuestBooking(event_id=event.id, email=email, name=name, count=count)
    db.add(gb)
    db.flush()
    # Capacity check AFTER insert (inside the transaction), see member booking.
    if services.count_booked(db, event.id) > event.max_participants:
        db.rollback()
        return back("Termin ist ausgebucht")
    db.commit()

    # Displayed maximum: the event only takes place at min_participants or
    # more, so the guest never pays more than the share at the minimum.
    participants = services.count_booked(db, event.id)
    divisor = max(participants, event.min_participants)
    max_share = services.per_person_share(event.normal_budget, divisor)
    total_price = max_share * count

    payee = services.payee_info(db, event.subscription)
    return TemplateResponse(
        "guest/confirmation.html",
        {
            "request": request,
            "name": name,
            "email": email,
            "count": count,
            "event": event,
            "subscription": event.subscription,
            "total_price": total_price,
            "paypal": payee["paypal"],
            "payee_name": payee["name"],
        },
    )
