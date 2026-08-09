"""Member routes: passwordless login, dashboard, bookings, super functions."""

import hmac
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import clock, services
from app.auth import (
    check_login_rate_limit,
    clear_session_cookie,
    create_session,
    destroy_session,
    require_member,
    require_super,
    set_session_cookie,
    SESSION_COOKIE,
)
from app.config import settings
from app.database import get_db
from app.emailer import (
    cancel_request_email_body,
    login_link_email_body,
    login_link_email_html,
    send_email,
    smtp_config_for,
)
from app.templates import TemplateResponse, format_date, format_euro
from app.models.models import (
    Booking,
    Event,
    GuestBooking,
    LoginToken,
    Member,
    Payment,
    WaitlistEntry,
    utcnow,
)

router = APIRouter()


def _redirect(msg: str, url: str = "/member/dashboard", mt: str = "success"):
    return RedirectResponse(url=f"{url}?msg={quote(msg)}&mt={mt}", status_code=302)


# ── Login (Magic Link per E-Mail) ──────────────────────────────────────────


@router.get("/login")
async def login_page(
    request: Request,
    msg: str = "",
    mt: str = "success",
    db: Session = Depends(get_db),
):
    # Bereits eingeloggt (z. B. App-Neustart) → direkt ins Dashboard
    from app.auth import get_current_member

    if get_current_member(request, db):
        return RedirectResponse(url="/member/dashboard", status_code=302)
    return TemplateResponse(
        "member/login.html", {"request": request, "msg": msg, "msg_type": mt}
    )


def _public_base_url(request: Request) -> str:
    """Base URL for links in emails: configured BASE_URL, else the request."""
    if settings.base_url:
        return settings.base_url.rstrip("/") + "/"
    return str(request.base_url)


@router.post("/login")
async def request_login_link(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    check_login_rate_limit(request, "member")
    members = (
        db.query(Member)
        .filter(Member.email == email, Member.is_active == True)  # noqa: E712
        .all()
    )

    # One shared 6-digit code per request (works for all memberships)
    code = f"{secrets.randbelow(10**6):06d}"
    base = _public_base_url(request)
    links = []
    for member in members:
        token = LoginToken(
            token=secrets.token_urlsafe(32),
            code=code,
            member_id=member.id,
            expires_at=utcnow() + timedelta(minutes=settings.login_token_minutes),
        )
        db.add(token)
        links.append(
            {
                "member": member,
                "url": f"{base}member/login/t/{token.token}",
            }
        )
    db.commit()

    sent = False
    if links:
        member = links[0]["member"]
        body_links = "\n".join(
            f"{l['member'].subscription.name}: {l['url']}" if len(links) > 1
            else l["url"]
            for l in links
        )
        sent = await send_email(
            member.subscription,
            email,
            "Dein Anmelde-Link – SportAbo",
            login_link_email_body(member.name, body_links, code),
            html=login_link_email_html(member.name, links[0]["url"], code),
        )

    # Dev fallback: without SMTP show link and code directly (never in prod)
    show_links = (
        links
        and not sent
        and settings.app_env != "production"
        and smtp_config_for(links[0]["member"].subscription) is None
    )
    return TemplateResponse(
        "member/login.html",
        {
            "request": request,
            "msg": (
                "Login-Link per E-Mail versendet – bitte Postfach prüfen."
                if sent
                else "Falls die E-Mail registriert ist, wurde ein Login-Link versendet."
            ),
            "msg_type": "success",
            "code_email": email,
            "dev_links": links if show_links else None,
            "dev_code": code if show_links else None,
        },
    )


@router.post("/login/code")
async def login_with_code(
    request: Request,
    email: str = Form(...),
    code: str = Form(..., min_length=6, max_length=6),
    db: Session = Depends(get_db),
):
    check_login_rate_limit(request, "member-code")
    now = utcnow()
    tokens = (
        db.query(LoginToken)
        .join(Member)
        .filter(
            Member.email == email,
            Member.is_active == True,  # noqa: E712
            LoginToken.expires_at >= now,
            LoginToken.code != "",
        )
        .all()
    )
    match = next(
        (t for t in tokens if hmac.compare_digest(t.code, code)), None
    )
    if not match:
        return TemplateResponse(
            "member/login.html",
            {
                "request": request,
                "error": "Code ungültig oder abgelaufen",
                "code_email": email,
            },
            status_code=401,
        )
    member = match.member
    # All tokens of this request are used up
    for t in tokens:
        if t.code == match.code:
            db.delete(t)
    session = create_session(db, member_id=member.id)
    resp = RedirectResponse(url="/member/dashboard", status_code=302)
    set_session_cookie(resp, session.token)
    return resp


@router.get("/login/t/{token}")
async def login_with_token(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
):
    row = db.query(LoginToken).filter(LoginToken.token == token).first()
    if not row or row.expires_at < utcnow():
        if row:
            db.delete(row)
            db.commit()
        return _redirect(
            "Login-Link ungültig oder abgelaufen – bitte neu anfordern.",
            "/member/login",
            mt="error",
        )
    member = row.member
    db.delete(row)
    if not member or not member.is_active:
        db.commit()
        return _redirect("Konto ist deaktiviert", "/member/login", mt="error")
    session = create_session(db, member_id=member.id)
    resp = RedirectResponse(url="/member/dashboard", status_code=302)
    set_session_cookie(resp, session.token)
    return resp


@router.get("/logout")
async def logout(request: Request, db: Session = Depends(get_db)):
    destroy_session(db, request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse(url="/member/login", status_code=302)
    clear_session_cookie(resp)
    return resp


# ── Dashboard ──────────────────────────────────────────────────────────────


@router.get("/dashboard")
async def dashboard(
    request: Request,
    msg: str = "",
    mt: str = "success",
    member: Member = Depends(require_member),
    db: Session = Depends(get_db),
):
    today = clock.today(db)
    upcoming_events = (
        db.query(Event)
        .filter(
            Event.subscription_id == member.subscription_id,
            Event.date >= today,
            Event.is_cancelled == False,  # noqa: E712
        )
        .order_by(Event.date)
        .all()
    )
    past_events = (
        db.query(Event)
        .filter(
            Event.subscription_id == member.subscription_id,
            Event.date < today,
            Event.is_cancelled == False,  # noqa: E712
        )
        .order_by(Event.date.desc())
        .limit(10)
        .all()
    )
    my_bookings = (
        db.query(Booking)
        .join(Event)
        .filter(Booking.member_id == member.id)
        .order_by(Event.date.desc())
        .all()
    )
    my_bookings_by_event = {b.event_id: b for b in my_bookings}
    booked_event_ids = set(my_bookings_by_event)
    all_events = upcoming_events + past_events
    booked_count = {e.id: services.count_booked(db, e.id) for e in all_events}
    free_by_event = {
        e.id: max(0, e.max_participants - booked_count[e.id])
        for e in upcoming_events
    }
    tiers_by_event = {
        e.id: services.price_tiers(e, limit=3) for e in upcoming_events
    }
    # Warteliste: Länge pro Termin + eigene Position (1-basiert)
    waitlist_count = {}
    waitlist_pos = {}
    for e in upcoming_events:
        entries = services.waitlist_entries(db, e.id)
        waitlist_count[e.id] = len(entries)
        for i, entry in enumerate(entries, start=1):
            if entry.member_id == member.id:
                waitlist_pos[e.id] = i
    # Own charge per past event (from the ledger)
    my_charges = {
        p.event_id: p.amount
        for p in db.query(Payment)
        .filter(Payment.member_id == member.id, Payment.type == Payment.TYPE_CHARGE)
        .all()
    }
    payments = (
        db.query(Payment)
        .filter(Payment.member_id == member.id)
        .order_by(Payment.created_at.desc())
        .limit(20)
        .all()
    )
    spending = services.member_spending(db, member.id)
    payee = (
        services.payee_info(db, member.subscription)
        if member.credit < 0
        else None
    )
    if payee and payee["member_id"] == member.id:
        payee = None  # der Zahlungsempfänger zahlt nicht an sich selbst
    fellow_members = (
        db.query(Member)
        .filter(
            Member.subscription_id == member.subscription_id,
            Member.id != member.id,
        )
        .order_by(Member.name)
        .all()
    )
    # Weitere Mitgliedschaften derselben Person (gleiche E-Mail) → Umschalter
    other_memberships = (
        db.query(Member)
        .filter(
            Member.email == member.email,
            Member.id != member.id,
            Member.is_active == True,  # noqa: E712
        )
        .all()
    )

    # Cancellation deadlines per upcoming event (hours before start)
    sub = member.subscription
    now = clock.now(db)
    cancel_state = {}
    for e in upcoming_events:
        start = datetime.combine(e.date, e.start_time)
        if e.settled_at:
            cancel_state[e.id] = "settled"
        elif now <= start - timedelta(hours=sub.cancel_hours_free):
            cancel_state[e.id] = "free"
        elif now <= start - timedelta(hours=sub.cancel_hours_approval):
            cancel_state[e.id] = "approval"
        else:
            cancel_state[e.id] = "closed"

    # Super members: pending cancellation requests + settleable events
    pending_requests = []
    settleable_events = []
    if member.is_super:
        pending_requests = (
            db.query(Booking)
            .join(Event)
            .filter(
                Event.subscription_id == member.subscription_id,
                Booking.cancel_requested_at.isnot(None),
            )
            .order_by(Event.date)
            .all()
        )
        settleable_events = [
            {
                "event": e,
                "blocker": services.settle_blocker(db, e),
                "booked": services.count_booked(db, e.id),
            }
            for e in db.query(Event)
            .filter(
                Event.subscription_id == member.subscription_id,
                Event.date < today,
                Event.is_cancelled == False,  # noqa: E712
                Event.settled_at.is_(None),
            )
            .order_by(Event.date)
            .all()
        ]

    return TemplateResponse(
        "member/dashboard.html",
        {
            "request": request,
            "member": member,
            "subscription": sub,
            "today": today,
            "date_override": clock.get_override(db),
            "upcoming_events": upcoming_events,
            "past_events": past_events,
            "my_bookings": my_bookings,
            "my_bookings_by_event": my_bookings_by_event,
            "booked_event_ids": booked_event_ids,
            "free_by_event": free_by_event,
            "payments": payments,
            "spending": spending,
            "payee": payee,
            "fellow_members": fellow_members,
            "other_memberships": other_memberships,
            "booked_count": booked_count,
            "tiers_by_event": tiers_by_event,
            "waitlist_count": waitlist_count,
            "waitlist_pos": waitlist_pos,
            "my_charges": my_charges,
            "cancel_state": cancel_state,
            "pending_requests": pending_requests,
            "settleable_events": settleable_events,
            "msg": msg,
            "msg_type": mt,
        },
    )


# ── Booking ────────────────────────────────────────────────────────────────


@router.post("/event/{event_id}/book")
async def book_event(
    request: Request,
    event_id: str,
    guest_count: int = Form(0, ge=0, le=20),
    guest_emails: str = Form(""),
    member: Member = Depends(require_member),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event or event.subscription_id != member.subscription_id:
        return _redirect("Termin nicht gefunden", mt="error")
    if event.is_cancelled:
        return _redirect("Termin ist abgesagt", mt="error")
    if event.date < clock.today(db):
        return _redirect("Vergangene Termine können nicht gebucht werden", mt="error")

    existing = (
        db.query(Booking)
        .filter(Booking.event_id == event_id, Booking.member_id == member.id)
        .first()
    )
    if existing:
        return _redirect("Du bist für diesen Termin bereits angemeldet", mt="error")

    booking = Booking(
        event_id=event_id,
        member_id=member.id,
        guest_count=guest_count,
        guest_emails=guest_emails,
    )
    db.add(booking)
    db.flush()
    if services.count_booked(db, event_id) > event.max_participants:
        db.rollback()
        return _redirect("Termin ist ausgebucht", mt="error")
    db.commit()
    guests = f" (+{guest_count} Gäste)" if guest_count else ""
    return _redirect(f"Anmeldung bestätigt{guests}")


@router.post("/event/{event_id}/unbook")
async def unbook_event(
    request: Request,
    event_id: str,
    member: Member = Depends(require_member),
    db: Session = Depends(get_db),
):
    booking = (
        db.query(Booking)
        .filter(Booking.event_id == event_id, Booking.member_id == member.id)
        .first()
    )
    if not booking:
        return _redirect("Keine Anmeldung gefunden", mt="error")
    event = booking.event
    if event.settled_at:
        return _redirect(
            "Termin ist bereits abgerechnet – Stornierung nicht mehr möglich",
            mt="error",
        )
    now = clock.now(db)
    start = datetime.combine(event.date, event.start_time)
    if now > start:
        return _redirect(
            "Der Termin hat bereits begonnen – Abmelden nicht mehr möglich",
            mt="error",
        )

    sub = member.subscription
    if now <= start - timedelta(hours=sub.cancel_hours_free):
        db.delete(booking)
        db.commit()
        promoted = await services.promote_from_waitlist(db, event)
        info = (
            f" – {promoted[0].name} rückt von der Warteliste nach"
            if promoted
            else ""
        )
        return _redirect(f"Abmeldung erfolgt{info}")

    if now <= start - timedelta(hours=sub.cancel_hours_approval):
        if booking.cancel_requested_at:
            return _redirect(
                "Abmelde-Anfrage läuft bereits – ein Super-Mitglied muss freigeben",
                mt="error",
            )
        booking.cancel_requested_at = utcnow()
        db.commit()
        # Notify all super members (best effort)
        supers = (
            db.query(Member)
            .filter(
                Member.subscription_id == sub.id,
                Member.is_super == True,  # noqa: E712
                Member.is_active == True,  # noqa: E712
            )
            .all()
        )
        for s in supers:
            await send_email(
                sub,
                s.email,
                f"Storno-Anfrage {format_date(event.date)} – {sub.name}",
                cancel_request_email_body(
                    s.name, member.name, event.date.strftime("%d.%m.%Y")
                ),
            )
        return _redirect(
            "Abmeldefrist abgelaufen – Anfrage wurde an die Super-Mitglieder gesendet"
        )

    return _redirect(
        f"Abmelden nicht mehr möglich (Frist: {sub.cancel_hours_approval} Stunden vor Termin)",
        mt="error",
    )


@router.post("/switch/{target_id}")
async def switch_membership(
    request: Request,
    target_id: str,
    member: Member = Depends(require_member),
    db: Session = Depends(get_db),
):
    """Zwischen eigenen Mitgliedschaften (gleiche E-Mail) wechseln:
    die bestehende Geräte-Session zeigt danach auf das andere Abo."""
    from app.auth import get_session

    target = db.query(Member).filter(Member.id == target_id).first()
    if (
        not target
        or target.email != member.email
        or not target.is_active
        or target.id == member.id
    ):
        return _redirect("Mitgliedschaft nicht gefunden", mt="error")
    session = get_session(db, request.cookies.get(SESSION_COOKIE))
    session.member_id = target.id
    db.commit()
    return _redirect(f"Gewechselt zu „{target.subscription.name}“")


@router.post("/transfer-received")
async def confirm_transfer_received(
    request: Request,
    payer_id: str = Form(...),
    amount: float = Form(..., gt=0),
    note: str = Form(""),
    member: Member = Depends(require_member),
    db: Session = Depends(get_db),
):
    """Zahlungseingang bestätigen: der Zahler bekommt Guthaben gutgeschrieben,
    das eigene Guthaben sinkt (Vorstreck-Modell). Betrugssicher, weil der
    Bestätigende sich damit nur selbst belasten kann."""
    from decimal import Decimal

    payer = db.query(Member).filter(Member.id == payer_id).first()
    if (
        not payer
        or payer.subscription_id != member.subscription_id
        or payer.id == member.id
    ):
        return _redirect("Ungültiger Zahler", mt="error")
    services.record_transfer(db, payer, member, Decimal(str(amount)), note)
    return _redirect(
        f"Zahlungseingang bestätigt: {payer.name} → du, {format_euro(amount)}"
    )


# ── Warteliste ─────────────────────────────────────────────────────────────


@router.post("/event/{event_id}/waitlist")
async def join_waitlist(
    request: Request,
    event_id: str,
    member: Member = Depends(require_member),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event or event.subscription_id != member.subscription_id:
        return _redirect("Termin nicht gefunden", mt="error")
    if event.is_cancelled or event.settled_at or event.date < clock.today(db):
        return _redirect("Für diesen Termin gibt es keine Warteliste", mt="error")
    if (
        db.query(Booking)
        .filter(Booking.event_id == event_id, Booking.member_id == member.id)
        .first()
    ):
        return _redirect("Du bist bereits angemeldet", mt="error")
    if services.free_spots(db, event) > 0:
        return _redirect("Es sind noch Plätze frei – melde dich direkt an", mt="error")
    if (
        db.query(WaitlistEntry)
        .filter(
            WaitlistEntry.event_id == event_id,
            WaitlistEntry.member_id == member.id,
        )
        .first()
    ):
        return _redirect("Du stehst bereits auf der Warteliste", mt="error")
    db.add(WaitlistEntry(event_id=event_id, member_id=member.id))
    db.commit()
    position = len(services.waitlist_entries(db, event_id))
    return _redirect(f"Auf der Warteliste – Platz {position}")


@router.post("/event/{event_id}/waitlist/leave")
async def leave_waitlist(
    request: Request,
    event_id: str,
    member: Member = Depends(require_member),
    db: Session = Depends(get_db),
):
    deleted = (
        db.query(WaitlistEntry)
        .filter(
            WaitlistEntry.event_id == event_id,
            WaitlistEntry.member_id == member.id,
        )
        .delete()
    )
    db.commit()
    if not deleted:
        return _redirect("Du stehst nicht auf der Warteliste", mt="error")
    return _redirect("Von der Warteliste ausgetragen")


# ── Teilnehmerliste (alle Mitglieder des Abos) ─────────────────────────────


@router.get("/event/{event_id}/participants")
async def event_participants(
    request: Request,
    event_id: str,
    member: Member = Depends(require_member),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event or event.subscription_id != member.subscription_id:
        return _redirect("Termin nicht gefunden", mt="error")
    bookings = db.query(Booking).filter(Booking.event_id == event_id).all()
    guest_bookings = (
        db.query(GuestBooking).filter(GuestBooking.event_id == event_id).all()
    )
    my_charge = (
        db.query(Payment)
        .filter(
            Payment.member_id == member.id,
            Payment.event_id == event_id,
            Payment.type == Payment.TYPE_CHARGE,
        )
        .first()
    )
    return TemplateResponse(
        "member/participants.html",
        {
            "request": request,
            "member": member,
            "event": event,
            "bookings": bookings,
            "guest_bookings": guest_bookings,
            "total_booked": services.count_booked(db, event_id),
            "shares": services.event_shares(db, event),
            "tiers": services.price_tiers(event),
            "my_charge": my_charge,
            "waitlist": services.waitlist_entries(db, event_id),
            "is_past": event.date < clock.today(db),
            "guest_link": (
                f"{_public_base_url(request)}g/{event.public_token}"
                if member.is_super
                else None
            ),
        },
    )


# ── Super-Member: Termine verwalten ────────────────────────────────────────


def _own_event(db: Session, event_id: str, member: Member) -> Event | None:
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event or event.subscription_id != member.subscription_id:
        return None
    return event


@router.post("/event/{event_id}/cancel")
async def super_cancel_event(
    request: Request,
    event_id: str,
    reduce_price: str = Form("no"),
    member: Member = Depends(require_super),
    db: Session = Depends(get_db),
):
    event = _own_event(db, event_id, member)
    if not event:
        return _redirect("Termin nicht gefunden", mt="error")
    if event.settled_at:
        return _redirect("Abgerechnete Termine können nicht abgesagt werden", mt="error")
    if event.is_cancelled:
        return _redirect("Termin ist bereits abgesagt", mt="error")
    services.cancel_event(db, event, reduce_price=(reduce_price == "yes"))
    if event.is_extra:
        msg = "Zusatztermin abgesagt"
    elif reduce_price == "yes":
        msg = f"Termin abgesagt – Gesamtpreis um {format_euro(event.abo_budget)} reduziert"
    else:
        msg = "Termin abgesagt – Budget auf die restlichen Termine umgelegt"
    return _redirect(msg)


@router.post("/event/{event_id}/settle")
async def super_settle_event(
    request: Request,
    event_id: str,
    member: Member = Depends(require_super),
    db: Session = Depends(get_db),
):
    event = _own_event(db, event_id, member)
    if not event:
        return _redirect("Termin nicht gefunden", mt="error")
    blocker = services.settle_blocker(db, event)
    if blocker:
        return _redirect(blocker, mt="error")
    charged, total, sent = await services.settle_and_notify(db, event)
    mail_info = f", {sent} E-Mails" if sent else ""
    return _redirect(
        f"{charged} Buchungen abgerechnet, gesamt {format_euro(total)}{mail_info}"
    )


@router.post("/cancel-request/{booking_id}/approve")
async def approve_cancel_request(
    request: Request,
    booking_id: str,
    member: Member = Depends(require_super),
    db: Session = Depends(get_db),
):
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if (
        not booking
        or booking.event.subscription_id != member.subscription_id
        or not booking.cancel_requested_at
    ):
        return _redirect("Anfrage nicht gefunden", mt="error")
    if booking.event.settled_at:
        return _redirect("Termin ist bereits abgerechnet", mt="error")
    name = booking.member.name
    event = booking.event
    db.delete(booking)
    db.commit()
    promoted = await services.promote_from_waitlist(db, event)
    info = (
        f" – {promoted[0].name} rückt von der Warteliste nach" if promoted else ""
    )
    return _redirect(f"Stornierung von {name} freigegeben{info}")


@router.post("/cancel-request/{booking_id}/reject")
async def reject_cancel_request(
    request: Request,
    booking_id: str,
    member: Member = Depends(require_super),
    db: Session = Depends(get_db),
):
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if (
        not booking
        or booking.event.subscription_id != member.subscription_id
        or not booking.cancel_requested_at
    ):
        return _redirect("Anfrage nicht gefunden", mt="error")
    booking.cancel_requested_at = None
    db.commit()
    return _redirect(f"Storno-Anfrage von {booking.member.name} abgelehnt")


@router.post("/extra-event")
async def super_create_extra_event(
    request: Request,
    event_date: str = Form(...),
    start_hour: int = Form(..., ge=0, le=23),
    start_minute: int = Form(0, ge=0, le=59),
    duration_minutes: int = Form(120, ge=1),
    budget: float = Form(..., ge=0),
    max_participants: int = Form(..., ge=1),
    min_participants: int = Form(..., ge=1),
    member: Member = Depends(require_super),
    db: Session = Depends(get_db),
):
    from decimal import Decimal
    from datetime import date as date_t, time as time_t

    from sqlalchemy.exc import IntegrityError

    try:
        services.create_extra_event(
            db,
            member.subscription,
            date_t.fromisoformat(event_date),
            time_t(hour=start_hour, minute=start_minute),
            duration_minutes,
            Decimal(str(budget)),
            max_participants,
            min_participants,
        )
    except IntegrityError:
        db.rollback()
        return _redirect("An diesem Tag existiert bereits ein Termin", mt="error")
    except ValueError:
        return _redirect("Ungültiges Datum", mt="error")
    return _redirect("Zusatztermin angelegt")


@router.post("/guest-booking/{gb_id}/paid")
async def super_guest_paid(
    request: Request,
    gb_id: str,
    amount: float = Form(..., gt=0),
    member: Member = Depends(require_super),
    db: Session = Depends(get_db),
):
    from decimal import Decimal

    gb = db.query(GuestBooking).filter(GuestBooking.id == gb_id).first()
    if not gb or gb.event.subscription_id != member.subscription_id:
        return _redirect("Gastbuchung nicht gefunden", mt="error")
    back = f"/member/event/{gb.event_id}/participants"
    if gb.paid_at:
        return _redirect("Bereits als bezahlt markiert", back, mt="error")
    recipient = services.mark_guest_paid(db, gb, Decimal(str(amount)))
    info = f" – Gegenbuchung bei {recipient.name}" if recipient else ""
    return _redirect(
        f"Zahlung von {gb.name} erfasst: {format_euro(amount)}{info}", back
    )


@router.post("/guest-booking/{gb_id}/unpaid")
async def super_guest_unpaid(
    request: Request,
    gb_id: str,
    member: Member = Depends(require_super),
    db: Session = Depends(get_db),
):
    gb = db.query(GuestBooking).filter(GuestBooking.id == gb_id).first()
    if not gb or gb.event.subscription_id != member.subscription_id:
        return _redirect("Gastbuchung nicht gefunden", mt="error")
    back = f"/member/event/{gb.event_id}/participants"
    if not gb.paid_at:
        return _redirect("Nicht als bezahlt markiert", back, mt="error")
    services.unmark_guest_paid(db, gb)
    return _redirect(f"Bezahlt-Markierung von {gb.name} storniert", back)
