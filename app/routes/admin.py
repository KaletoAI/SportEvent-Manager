"""Admin routes: login, subscription management, events, settlement, stats."""

import hmac
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock, services
from app.auth import (
    SESSION_COOKIE,
    check_login_rate_limit,
    clear_session_cookie,
    create_session,
    destroy_session,
    get_session,
    require_admin,
    set_session_cookie,
)
from app.config import settings
from app.database import get_db
from app.templates import TemplateResponse, format_euro
from app.web import flash_redirect, parse_clock, public_base_url
from app.models.models import (
    Booking,
    Event,
    GuestBooking,
    LoginToken,
    Member,
    Payment,
    Person,
    Subscription,
    UserSession,
    WaitlistEntry,
)

router = APIRouter()


def _redirect(msg: str, url: str = "/admin/dashboard", mt: str = "success"):
    """Redirect with typed flash message via URL params."""
    return flash_redirect(msg, url, mt)


# ── Login ──────────────────────────────────────────────────────────────────


@router.get("/login")
async def login_page(request: Request, db: Session = Depends(get_db)):
    # Bereits eingeloggt → direkt ins Dashboard
    session = get_session(db, request.cookies.get(SESSION_COOKIE))
    if session and session.is_admin:
        return RedirectResponse(url="/admin/dashboard", status_code=302)
    return TemplateResponse("admin/login.html", {"request": request})


@router.post("/login")
async def login(
    request: Request,
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    check_login_rate_limit(request, "admin")
    if hmac.compare_digest(password, settings.admin_password):
        session = create_session(db, is_admin=True)
        resp = RedirectResponse(url="/admin/dashboard", status_code=302)
        set_session_cookie(resp, session.token)
        return resp
    return TemplateResponse(
        "admin/login.html",
        {"request": request, "error": "Falsches Passwort"},
        status_code=401,
    )


@router.get("/logout")
async def logout(request: Request, db: Session = Depends(get_db)):
    destroy_session(db, request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse(url="/admin/login", status_code=302)
    clear_session_cookie(resp)
    return resp


# ── Subscription CRUD ──────────────────────────────────────────────────────


@router.get("/subscription/new", dependencies=[Depends(require_admin)])
async def new_subscription_page(request: Request):
    return TemplateResponse(
        "admin/subscription_form.html",
        {"request": request, "subscription": None},
    )


def _apply_subscription_form(sub: Subscription, f: dict) -> Optional[str]:
    """Validate the Abo form and copy it onto `sub`; returns an error
    message instead when something doesn't fit."""
    try:
        start = parse_clock(f["start_time"], f["start_hour"], f["start_minute"])
        first = date.fromisoformat(f["start_date"])
        last = date.fromisoformat(f["end_date"])
    except ValueError:
        return "Ungültiges Datum oder Uhrzeit"
    if last < first:
        return "Das Enddatum liegt vor dem Startdatum"
    if last > first + timedelta(days=3 * 366):
        return "Ein Abo darf höchstens drei Jahre umfassen"
    if f["min_participants"] > f["max_participants"]:
        return "Mindestzahl darf das Maximum nicht übersteigen"
    sub.name = f["name"].strip()
    sub.description = f["description"]
    sub.weekday = f["weekday"]
    sub.start_time = start
    sub.duration_minutes = f["duration_minutes"]
    sub.start_date = first
    sub.end_date = last
    sub.default_price = Decimal(str(f["default_price"]))
    sub.abo_price = Decimal(str(f["abo_price"]))
    sub.max_participants = f["max_participants"]
    sub.min_participants = f["min_participants"]
    sub.cancel_hours_free = f["cancel_hours_free"]
    sub.cancel_hours_approval = f["cancel_hours_approval"]
    sub.paypal_address = f["paypal_address"].strip()
    sub.payout_mode = (
        f["payout_mode"] if f["payout_mode"] in ("central", "member") else "central"
    )
    return None


@router.post("/subscription/new", dependencies=[Depends(require_admin)])
async def create_subscription(
    request: Request,
    name: str = Form(..., max_length=200),
    description: str = Form("", max_length=2000),
    weekday: int = Form(..., ge=0, le=6),
    start_time: str = Form(""),
    start_hour: int | None = Form(None, ge=0, le=23),
    start_minute: int = Form(0, ge=0, le=59),
    duration_minutes: int = Form(120, ge=1, le=24 * 60),
    start_date: str = Form(...),
    end_date: str = Form(...),
    default_price: float = Form(10.0, ge=0, le=1000000),
    abo_price: float = Form(8.0, ge=0, le=1000000),
    max_participants: int = Form(12, ge=1, le=500),
    min_participants: int = Form(4, ge=1, le=500),
    cancel_hours_free: int = Form(48, ge=0, le=24 * 60),
    cancel_hours_approval: int = Form(0, ge=0, le=24 * 60),
    paypal_address: str = Form("", max_length=200),
    payout_mode: str = Form("central"),
    db: Session = Depends(get_db),
):
    form = dict(locals())
    sub = Subscription()
    error = _apply_subscription_form(sub, form)
    if error:
        return TemplateResponse(
            "admin/subscription_form.html",
            {"request": request, "subscription": None, "error": error},
            status_code=400,
        )
    db.add(sub)
    db.commit()
    return _redirect(f"Abo „{sub.name}“ angelegt")


@router.get("/subscription/{sub_id}/edit", dependencies=[Depends(require_admin)])
async def edit_subscription_page(
    request: Request,
    sub_id: str,
    db: Session = Depends(get_db),
):
    sub = db.query(Subscription).filter(Subscription.id == sub_id).first()
    if not sub:
        return _redirect("Abo nicht gefunden", mt="error")
    return TemplateResponse(
        "admin/subscription_form.html",
        {"request": request, "subscription": sub},
    )


@router.post("/subscription/{sub_id}/edit", dependencies=[Depends(require_admin)])
async def update_subscription(
    request: Request,
    sub_id: str,
    name: str = Form(..., max_length=200),
    description: str = Form("", max_length=2000),
    weekday: int = Form(..., ge=0, le=6),
    start_time: str = Form(""),
    start_hour: int | None = Form(None, ge=0, le=23),
    start_minute: int = Form(0, ge=0, le=59),
    duration_minutes: int = Form(120, ge=1, le=24 * 60),
    start_date: str = Form(...),
    end_date: str = Form(...),
    default_price: float = Form(10.0, ge=0, le=1000000),
    abo_price: float = Form(8.0, ge=0, le=1000000),
    max_participants: int = Form(12, ge=1, le=500),
    min_participants: int = Form(4, ge=1, le=500),
    cancel_hours_free: int = Form(48, ge=0, le=24 * 60),
    cancel_hours_approval: int = Form(0, ge=0, le=24 * 60),
    paypal_address: str = Form("", max_length=200),
    payout_mode: str = Form("central"),
    db: Session = Depends(get_db),
):
    form = dict(locals())
    sub = db.query(Subscription).filter(Subscription.id == sub_id).first()
    if not sub:
        return _redirect("Abo nicht gefunden", mt="error")
    error = _apply_subscription_form(sub, form)
    if error:
        db.rollback()
        return _redirect(error, f"/admin/subscription/{sub_id}/edit", mt="error")
    # Totals may have changed → redistribute over the open events
    services.recompute_budgets(db, sub)
    db.commit()
    return _redirect(f"Abo „{sub.name}“ gespeichert")


@router.post(
    "/subscription/{sub_id}/delete", dependencies=[Depends(require_admin)]
)
async def delete_subscription(
    request: Request,
    sub_id: str,
    confirm_name: str = Form(""),
    db: Session = Depends(get_db),
):
    """Abo endgültig löschen — Name muss zur Bestätigung eingetippt werden."""
    sub = db.query(Subscription).filter(Subscription.id == sub_id).first()
    if not sub:
        return _redirect("Abo nicht gefunden", mt="error")
    if confirm_name.strip() != sub.name:
        return _redirect(
            "Löschen abgebrochen: Der eingegebene Name stimmt nicht überein",
            f"/admin/subscription/{sub_id}/edit",
            mt="error",
        )
    name = sub.name
    services.delete_subscription(db, sub)
    return _redirect(f"Abo „{name}“ und alle zugehörigen Daten gelöscht")


# ── Event Generation ─────────────────────────────────────────────────────


@router.post(
    "/subscription/{sub_id}/generate-events",
    dependencies=[Depends(require_admin)],
)
async def generate_events(
    request: Request,
    sub_id: str,
    db: Session = Depends(get_db),
):
    sub = db.query(Subscription).filter(Subscription.id == sub_id).first()
    if not sub:
        return _redirect("Abo nicht gefunden", mt="error")

    existing_dates = {
        d
        for (d,) in db.query(Event.date)
        .filter(Event.subscription_id == sub.id)
        .all()
    }
    current = sub.start_date
    created = 0
    while current <= sub.end_date:
        if current.weekday() == sub.weekday and current not in existing_dates:
            start_dt = datetime.combine(current, sub.start_time)
            end_dt = start_dt + timedelta(minutes=sub.duration_minutes)
            db.add(
                Event(
                    subscription_id=sub.id,
                    date=current,
                    start_time=sub.start_time,
                    end_time=end_dt.time(),
                    max_participants=sub.max_participants,
                    min_participants=sub.min_participants,
                )
            )
            created += 1
        current += timedelta(days=1)

    db.flush()
    db.refresh(sub)
    services.recompute_budgets(db, sub)
    db.commit()
    return _redirect(
        f"{created} Termine erstellt", f"/admin/subscription/{sub_id}"
    )


# ── Dashboard ──────────────────────────────────────────────────────────────


@router.get("/", dependencies=[Depends(require_admin)])
async def admin_root():
    return RedirectResponse(url="/admin/dashboard")


@router.get("/dashboard", dependencies=[Depends(require_admin)])
async def dashboard(
    request: Request,
    msg: str = "",
    mt: str = "success",
    db: Session = Depends(get_db),
):
    subs = db.query(Subscription).order_by(Subscription.created_at.desc()).all()
    return TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "subscriptions": subs,
            "date_override": clock.get_override(db),
            "today": clock.today(db),
            "msg": msg,
            "msg_type": mt,
        },
    )


# ── Test-Datum (Debug) ────────────────────────────────────────────────────


@router.post("/test-date", dependencies=[Depends(require_admin)])
async def set_test_date(
    request: Request,
    test_date: str = Form(""),
    db: Session = Depends(get_db),
):
    """Temporarily override the system date (empty value = reset)."""
    if not test_date:
        clock.set_override(db, None)
        return _redirect("Test-Datum zurückgesetzt – System nutzt wieder das echte Datum")
    try:
        value = date.fromisoformat(test_date)
    except ValueError:
        return _redirect("Ungültiges Datum", mt="error")
    clock.set_override(db, value)
    return _redirect(f"Test-Datum aktiv: {value.strftime('%d.%m.%Y')}")


# ── Subscription Detail ──────────────────────────────────────────────────


@router.get("/subscription/{sub_id}", dependencies=[Depends(require_admin)])
async def subscription_detail(
    request: Request,
    sub_id: str,
    msg: str = "",
    mt: str = "success",
    db: Session = Depends(get_db),
):
    sub = db.query(Subscription).filter(Subscription.id == sub_id).first()
    if not sub:
        return _redirect("Abo nicht gefunden", mt="error")
    events = (
        db.query(Event)
        .filter(Event.subscription_id == sub_id)
        .order_by(Event.date)
        .all()
    )
    members = (
        db.query(Member)
        .filter(Member.subscription_id == sub_id)
        .order_by(Member.name)
        .all()
    )
    booked_by_event = services.booked_counts(db, [e.id for e in events])
    today = clock.today(db)
    return TemplateResponse(
        "admin/subscription_detail.html",
        {
            "request": request,
            "subscription": sub,
            "events": events,
            "members": members,
            "booked_by_event": booked_by_event,
            "today": today,
            "date_override": clock.get_override(db),
            "msg": msg,
            "msg_type": mt,
        },
    )


# ── Member Management ────────────────────────────────────────────────────


@router.get(
    "/subscription/{sub_id}/members/new", dependencies=[Depends(require_admin)]
)
async def new_member_page(
    request: Request,
    sub_id: str,
    db: Session = Depends(get_db),
):
    sub = db.query(Subscription).filter(Subscription.id == sub_id).first()
    if not sub:
        return _redirect("Abo nicht gefunden", mt="error")
    existing_emails = {
        e
        for (e,) in db.query(Member.email)
        .filter(Member.subscription_id == sub_id)
        .all()
    }
    persons_available = [
        p
        for p in db.query(Person).order_by(Person.name).all()
        if p.email not in existing_emails
    ]
    return TemplateResponse(
        "admin/member_form.html",
        {
            "request": request,
            "subscription": sub,
            "member": None,
            "persons_available": persons_available,
        },
    )


@router.post(
    "/subscription/{sub_id}/members/new", dependencies=[Depends(require_admin)]
)
async def create_member(
    request: Request,
    sub_id: str,
    name: str = Form(..., max_length=200),
    email: str = Form(..., max_length=200),
    paypal_address: str = Form("", max_length=200),
    credit: float = Form(0.0, ge=-100000, le=100000),
    db: Session = Depends(get_db),
):
    sub = db.query(Subscription).filter(Subscription.id == sub_id).first()
    if not sub:
        return _redirect("Abo nicht gefunden", mt="error")
    name = name.strip()
    email = services.normalize_email(email)
    paypal_address = paypal_address.strip()
    existing = (
        db.query(Member)
        .filter(Member.subscription_id == sub_id, Member.email == email)
        .first()
    )
    if existing:
        return TemplateResponse(
            "admin/member_form.html",
            {
                "request": request,
                "subscription": sub,
                "member": None,
                "error": "E-Mail existiert bereits in diesem Abo",
            },
            status_code=400,
        )
    member = Member(
        subscription_id=sub_id,
        name=name,
        email=email,
        paypal_address=paypal_address,
        credit=Decimal("0.00"),
    )
    db.add(member)
    services.upsert_person(db, name, email, paypal_address)
    db.flush()
    start_credit = Decimal(str(credit))
    if start_credit:
        db.add(
            Payment(
                member_id=member.id,
                amount=start_credit,
                type=Payment.TYPE_DEPOSIT,
                note="Startguthaben",
            )
        )
        member.credit = start_credit
    db.commit()
    return _redirect(
        f"Mitglied „{name}“ angelegt", f"/admin/subscription/{sub_id}"
    )


@router.post(
    "/subscription/{sub_id}/members/add-existing",
    dependencies=[Depends(require_admin)],
)
async def add_existing_member(
    request: Request,
    sub_id: str,
    person_id: str = Form(...),
    credit: float = Form(0.0, ge=-100000, le=100000),
    db: Session = Depends(get_db),
):
    """Mitglied aus der zentralen Nutzerablage in dieses Abo übernehmen."""
    sub = db.query(Subscription).filter(Subscription.id == sub_id).first()
    if not sub:
        return _redirect("Abo nicht gefunden", mt="error")
    person = db.query(Person).filter(Person.id == person_id).first()
    if not person:
        return _redirect(
            "Person nicht gefunden",
            f"/admin/subscription/{sub_id}/members/new",
            mt="error",
        )
    existing = (
        db.query(Member)
        .filter(
            Member.subscription_id == sub_id,
            func.lower(Member.email) == services.normalize_email(person.email),
        )
        .first()
    )
    if existing:
        return _redirect(
            f"{person.name} ist bereits Mitglied dieses Abos",
            f"/admin/subscription/{sub_id}/members/new",
            mt="error",
        )
    member = Member(
        subscription_id=sub_id,
        name=person.name,
        email=services.normalize_email(person.email),
        paypal_address=person.paypal_address or "",
        credit=Decimal("0.00"),
    )
    db.add(member)
    db.flush()
    start_credit = Decimal(str(credit))
    if start_credit:
        db.add(
            Payment(
                member_id=member.id,
                amount=start_credit,
                type=Payment.TYPE_DEPOSIT,
                note="Startguthaben",
            )
        )
        member.credit = start_credit
    db.commit()
    return _redirect(
        f"Mitglied „{person.name}“ übernommen", f"/admin/subscription/{sub_id}"
    )


@router.get("/member/{member_id}/edit", dependencies=[Depends(require_admin)])
async def edit_member_page(
    request: Request,
    member_id: str,
    db: Session = Depends(get_db),
):
    member = db.query(Member).filter(Member.id == member_id).first()
    if not member:
        return _redirect("Mitglied nicht gefunden", mt="error")
    payments = (
        db.query(Payment)
        .filter(Payment.member_id == member_id)
        .order_by(Payment.created_at.desc())
        .all()
    )
    other_members = (
        db.query(Member)
        .filter(
            Member.subscription_id == member.subscription_id,
            Member.id != member_id,
        )
        .order_by(Member.name)
        .all()
    )
    bookings_count = (
        db.query(Booking).filter(Booking.member_id == member_id).count()
    )
    return TemplateResponse(
        "admin/member_form.html",
        {
            "request": request,
            "subscription": member.subscription,
            "member": member,
            "payments": payments,
            "other_members": other_members,
            "deletable": bookings_count == 0 and not payments,
        },
    )


@router.post("/member/{member_id}/edit", dependencies=[Depends(require_admin)])
async def update_member(
    request: Request,
    member_id: str,
    name: str = Form(..., max_length=200),
    email: str = Form(..., max_length=200),
    paypal_address: str = Form("", max_length=200),
    is_active: str | None = Form(None),
    is_super: str | None = Form(None),
    db: Session = Depends(get_db),
):
    member = db.query(Member).filter(Member.id == member_id).first()
    if not member:
        return _redirect("Mitglied nicht gefunden", mt="error")
    name = name.strip()
    email = services.normalize_email(email)
    paypal_address = paypal_address.strip()
    duplicate = (
        db.query(Member)
        .filter(
            Member.subscription_id == member.subscription_id,
            Member.email == email,
            Member.id != member_id,
        )
        .first()
    )
    if duplicate:
        return _redirect(
            f"E-Mail {email} wird in diesem Abo bereits von "
            f"„{duplicate.name}“ verwendet",
            f"/admin/member/{member_id}/edit",
            mt="error",
        )
    member.name = name
    member.email = email
    member.paypal_address = paypal_address
    # Unchecked checkboxes are absent from the form body
    member.is_active = is_active is not None
    member.is_super = is_super is not None
    if not member.is_active:
        # Deactivated members don't move up from waitlists any more
        db.query(WaitlistEntry).filter(WaitlistEntry.member_id == member.id).delete(
            synchronize_session=False
        )
    services.upsert_person(db, name, email, paypal_address)
    db.commit()
    return _redirect(
        f"Mitglied „{name}“ gespeichert",
        f"/admin/subscription/{member.subscription_id}",
    )


@router.post(
    "/member/{member_id}/add-credit", dependencies=[Depends(require_admin)]
)
async def add_member_credit(
    request: Request,
    member_id: str,
    amount: float = Form(..., ge=-100000, le=100000),
    note: str = Form("", max_length=200),
    db: Session = Depends(get_db),
):
    member = db.query(Member).filter(Member.id == member_id).first()
    if not member:
        return _redirect("Mitglied nicht gefunden", mt="error")
    delta = Decimal(str(amount))
    if not delta:
        return _redirect(
            "Betrag darf nicht 0 sein",
            f"/admin/member/{member_id}/edit",
            mt="error",
        )
    db.add(
        Payment(
            member_id=member_id,
            amount=delta,
            type=Payment.TYPE_DEPOSIT,
            note=note or "Einzahlung",
        )
    )
    member.credit += delta
    db.commit()
    return _redirect(
        f"Guthaben gebucht: {format_euro(delta)}",
        f"/admin/member/{member_id}/edit",
    )


@router.post(
    "/member/{member_id}/delete", dependencies=[Depends(require_admin)]
)
async def delete_member(
    request: Request,
    member_id: str,
    db: Session = Depends(get_db),
):
    """Mitglied endgültig löschen — nur ohne Buchungen und Kontobewegungen
    (sonst würde die Abrechnungs-Historie zerstört; dann deaktivieren)."""
    member = db.query(Member).filter(Member.id == member_id).first()
    if not member:
        return _redirect("Mitglied nicht gefunden", mt="error")
    bookings = db.query(Booking).filter(Booking.member_id == member_id).count()
    payments = db.query(Payment).filter(Payment.member_id == member_id).count()
    if bookings or payments:
        return _redirect(
            f"„{member.name}“ hat {bookings} Anmeldungen und {payments} "
            "Kontobewegungen – bitte stattdessen deaktivieren",
            f"/admin/member/{member_id}/edit",
            mt="error",
        )
    sub_id = member.subscription_id
    name = member.name
    # Sessions, Login-Tokens und Wartelisten-Plätze des Mitglieds mit entfernen
    db.query(UserSession).filter(UserSession.member_id == member_id).delete()
    db.query(LoginToken).filter(LoginToken.member_id == member_id).delete()
    db.query(WaitlistEntry).filter(WaitlistEntry.member_id == member_id).delete()
    db.delete(member)
    db.commit()
    return _redirect(
        f"Mitglied „{name}“ gelöscht (bleibt in der Nutzerablage)",
        f"/admin/subscription/{sub_id}",
    )


@router.post(
    "/member/{member_id}/transfer", dependencies=[Depends(require_admin)]
)
async def record_member_transfer(
    request: Request,
    member_id: str,
    payee_id: str = Form(...),
    amount: float = Form(..., gt=0, le=100000),
    note: str = Form("", max_length=200),
    db: Session = Depends(get_db),
):
    """Zahlung zwischen Mitgliedern erfassen (Vorstreck-Modell):
    dieses Mitglied hat an das Empfaenger-Mitglied gezahlt."""
    payer = db.query(Member).filter(Member.id == member_id).first()
    payee = db.query(Member).filter(Member.id == payee_id).first()
    if not payer or not payee:
        return _redirect("Mitglied nicht gefunden", mt="error")
    if payer.subscription_id != payee.subscription_id or payer.id == payee.id:
        return _redirect(
            "Ungültiger Empfänger",
            f"/admin/member/{member_id}/edit",
            mt="error",
        )
    services.record_transfer(db, payer, payee, Decimal(str(amount)), note)
    return _redirect(
        f"Zahlung erfasst: {payer.name} → {payee.name} {format_euro(amount)}",
        f"/admin/member/{member_id}/edit",
    )


# ── Event Management ─────────────────────────────────────────────────────


@router.get("/event/{event_id}", dependencies=[Depends(require_admin)])
async def event_detail(
    request: Request,
    event_id: str,
    msg: str = "",
    mt: str = "success",
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        return _redirect("Termin nicht gefunden", mt="error")
    bookings = db.query(Booking).filter(Booking.event_id == event_id).all()
    guest_bookings = (
        db.query(GuestBooking).filter(GuestBooking.event_id == event_id).all()
    )
    booked_member_ids = {b.member_id for b in bookings}
    available_members = [
        m
        for m in db.query(Member)
        .filter(Member.subscription_id == event.subscription_id, Member.is_active)
        .order_by(Member.name)
        .all()
        if m.id not in booked_member_ids
    ]
    total_booked = services.count_booked(db, event_id)
    shares = services.event_shares(db, event)
    bookings_total = sum(
        (
            shares["member_share"] + b.guest_count * shares["guest_share"]
            for b in bookings
        ),
        Decimal("0.00"),
    )
    guest_link = f"{public_base_url(request)}g/{event.public_token}"
    return TemplateResponse(
        "admin/event_detail.html",
        {
            "request": request,
            "event": event,
            "subscription": event.subscription,
            "bookings": bookings,
            "guest_bookings": guest_bookings,
            "available_members": available_members,
            "total_booked": total_booked,
            "bookings_total": bookings_total,
            "guest_link": guest_link,
            "shares": shares,
            "waitlist": services.waitlist_entries(db, event_id),
            "tiers": services.price_tiers(event),
            "settle_blocker": services.settle_blocker(db, event),
            "today": clock.today(db),
            "date_override": clock.get_override(db),
            "msg": msg,
            "msg_type": mt,
        },
    )


@router.post("/event/{event_id}/cancel", dependencies=[Depends(require_admin)])
async def cancel_event(
    request: Request,
    event_id: str,
    reduce_price: str = Form("no"),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        return _redirect("Termin nicht gefunden", mt="error")
    back = f"/admin/event/{event_id}"
    if event.settled_at:
        return _redirect("Abgerechnete Termine können nicht abgesagt werden", back, mt="error")
    # Idempotent: a double tap must not flip the event back
    if event.is_cancelled:
        return _redirect("Termin ist bereits abgesagt", back, mt="error")
    services.cancel_event(db, event, reduce_price=(reduce_price == "yes"))
    if event.is_extra:
        msg = "Zusatztermin abgesagt"
    elif reduce_price == "yes":
        msg = (
            f"Termin abgesagt – Gesamtpreis um {format_euro(event.abo_budget)} reduziert"
        )
    else:
        msg = "Termin abgesagt – Budget auf die restlichen Termine umgelegt"
    return _redirect(msg, back)


@router.post("/event/{event_id}/reactivate", dependencies=[Depends(require_admin)])
async def reactivate_event(
    request: Request,
    event_id: str,
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        return _redirect("Termin nicht gefunden", mt="error")
    back = f"/admin/event/{event_id}"
    if not event.is_cancelled:
        return _redirect("Termin ist nicht abgesagt", back, mt="error")
    services.reactivate_event(db, event)
    return _redirect("Termin reaktiviert", back)


@router.post("/event/{event_id}/capacity", dependencies=[Depends(require_admin)])
async def update_event_capacity(
    request: Request,
    event_id: str,
    min_participants: int = Form(..., ge=1, le=500),
    max_participants: int = Form(..., ge=1, le=500),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        return _redirect("Termin nicht gefunden", mt="error")
    if min_participants > max_participants:
        return _redirect(
            "Mindestzahl darf Maximum nicht übersteigen",
            f"/admin/event/{event_id}",
            mt="error",
        )
    event.min_participants = min_participants
    event.max_participants = max_participants
    db.commit()
    promoted = await services.promote_from_waitlist(db, event)
    info = f" – {len(promoted)} von der Warteliste nachgerückt" if promoted else ""
    return _redirect(
        f"Teilnehmergrenzen: {min_participants}–{max_participants}{info}",
        f"/admin/event/{event_id}",
    )


@router.post("/event/{event_id}/settle", dependencies=[Depends(require_admin)])
async def settle_event(
    request: Request,
    event_id: str,
    db: Session = Depends(get_db),
):
    """Charge all member bookings for this event and email the members."""
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        return _redirect("Termin nicht gefunden", mt="error")
    blocker = services.settle_blocker(db, event)
    if blocker:
        return _redirect(blocker, f"/admin/event/{event_id}", mt="error")

    charged, total, sent = await services.settle_and_notify(db, event)
    mail_info = (
        f", {sent} E-Mails versendet"
        if sent
        else " (kein SMTP konfiguriert – keine E-Mails)"
    )
    return _redirect(
        f"{charged} Buchungen abgerechnet, gesamt {format_euro(total)}{mail_info}",
        f"/admin/event/{event_id}",
    )


@router.post(
    "/subscription/{sub_id}/extra-event", dependencies=[Depends(require_admin)]
)
async def create_extra_event(
    request: Request,
    sub_id: str,
    event_date: str = Form(...),
    start_time: str = Form(""),
    start_hour: int | None = Form(None, ge=0, le=23),
    start_minute: int = Form(0, ge=0, le=59),
    duration_minutes: int = Form(120, ge=1, le=24 * 60),
    budget: float = Form(..., ge=0, le=100000),
    max_participants: int = Form(..., ge=1, le=500),
    min_participants: int = Form(..., ge=1, le=500),
    db: Session = Depends(get_db),
):
    sub = db.query(Subscription).filter(Subscription.id == sub_id).first()
    if not sub:
        return _redirect("Abo nicht gefunden", mt="error")
    back = f"/admin/subscription/{sub_id}"
    try:
        day = date.fromisoformat(event_date)
        start = parse_clock(start_time, start_hour, start_minute)
    except ValueError:
        return _redirect("Ungültiges Datum oder Uhrzeit", back, mt="error")
    if min_participants > max_participants:
        return _redirect("Mindestzahl darf das Maximum nicht übersteigen", back, mt="error")
    try:
        services.create_extra_event(
            db,
            sub,
            day,
            start,
            duration_minutes,
            Decimal(str(budget)),
            max_participants,
            min_participants,
        )
    except IntegrityError:
        db.rollback()
        return _redirect("An diesem Tag existiert bereits ein Termin", back, mt="error")
    return _redirect("Zusatztermin angelegt", back)


# ── Buchungen durch den Admin (Test & Verwaltung) ─────────────────────────


@router.post("/event/{event_id}/book-member", dependencies=[Depends(require_admin)])
async def admin_book_member(
    request: Request,
    event_id: str,
    member_id: str = Form(...),
    guest_count: int = Form(0, ge=0, le=20),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        return _redirect("Termin nicht gefunden", mt="error")
    back = f"/admin/event/{event_id}"
    if event.settled_at:
        return _redirect("Termin ist bereits abgerechnet", back, mt="error")
    if event.is_cancelled:
        return _redirect("Termin ist abgesagt", back, mt="error")
    member = db.query(Member).filter(Member.id == member_id).first()
    if not member or member.subscription_id != event.subscription_id:
        return _redirect("Mitglied nicht gefunden", back, mt="error")
    existing = (
        db.query(Booking)
        .filter(Booking.event_id == event_id, Booking.member_id == member_id)
        .first()
    )
    if existing:
        return _redirect(
            f"{member.name} ist bereits angemeldet", back, mt="error"
        )
    db.add(Booking(event_id=event_id, member_id=member_id, guest_count=guest_count))
    db.flush()
    if services.count_booked(db, event_id) > event.max_participants:
        db.rollback()
        return _redirect("Termin ist ausgebucht", back, mt="error")
    db.query(WaitlistEntry).filter(
        WaitlistEntry.event_id == event_id, WaitlistEntry.member_id == member_id
    ).delete(synchronize_session=False)
    db.commit()
    return _redirect(f"{member.name} angemeldet", back)


@router.post("/event/{event_id}/book-guest", dependencies=[Depends(require_admin)])
async def admin_book_guest(
    request: Request,
    event_id: str,
    name: str = Form(..., min_length=1, max_length=100),
    email: str = Form("", max_length=200),
    count: int = Form(1, ge=1, le=20),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        return _redirect("Termin nicht gefunden", mt="error")
    back = f"/admin/event/{event_id}"
    name = name.strip()
    email = services.normalize_email(email)
    if event.settled_at:
        return _redirect("Termin ist bereits abgerechnet", back, mt="error")
    if event.is_cancelled:
        return _redirect("Termin ist abgesagt", back, mt="error")
    db.add(GuestBooking(event_id=event_id, name=name, email=email, count=count))
    db.flush()
    if services.count_booked(db, event_id) > event.max_participants:
        db.rollback()
        return _redirect("Termin ist ausgebucht", back, mt="error")
    db.commit()
    return _redirect(f"Gast „{name}“ ({count} Pers.) angemeldet", back)


@router.post("/booking/{booking_id}/delete", dependencies=[Depends(require_admin)])
async def admin_delete_booking(
    request: Request,
    booking_id: str,
    db: Session = Depends(get_db),
):
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        return _redirect("Anmeldung nicht gefunden", mt="error")
    back = f"/admin/event/{booking.event_id}"
    if booking.event.settled_at:
        return _redirect("Termin ist bereits abgerechnet", back, mt="error")
    event = booking.event
    db.delete(booking)
    db.commit()
    promoted = await services.promote_from_waitlist(db, event)
    info = f" – {promoted[0].name} rückt nach" if promoted else ""
    return _redirect(f"Anmeldung entfernt{info}", back)


@router.post(
    "/guest-booking/{gb_id}/delete", dependencies=[Depends(require_admin)]
)
async def admin_delete_guest_booking(
    request: Request,
    gb_id: str,
    db: Session = Depends(get_db),
):
    gb = db.query(GuestBooking).filter(GuestBooking.id == gb_id).first()
    if not gb:
        return _redirect("Gastbuchung nicht gefunden", mt="error")
    back = f"/admin/event/{gb.event_id}"
    if gb.event.settled_at:
        return _redirect("Termin ist bereits abgerechnet", back, mt="error")
    if gb.paid_at:
        return _redirect(
            "Gastbuchung ist als bezahlt markiert – erst die Bezahlt-Markierung zurücknehmen",
            back,
            mt="error",
        )
    event = gb.event
    db.delete(gb)
    db.commit()
    promoted = await services.promote_from_waitlist(db, event)
    info = f" – {promoted[0].name} rückt nach" if promoted else ""
    return _redirect(f"Gastbuchung entfernt{info}", back)


@router.post(
    "/guest-booking/{gb_id}/paid", dependencies=[Depends(require_admin)]
)
async def admin_guest_paid(
    request: Request,
    gb_id: str,
    amount: float = Form(..., gt=0, le=100000),
    db: Session = Depends(get_db),
):
    gb = db.query(GuestBooking).filter(GuestBooking.id == gb_id).first()
    if not gb:
        return _redirect("Gastbuchung nicht gefunden", mt="error")
    back = f"/admin/event/{gb.event_id}"
    if gb.paid_at:
        return _redirect("Bereits als bezahlt markiert", back, mt="error")
    recipient = services.mark_guest_paid(db, gb, Decimal(str(amount)))
    info = f" – Gegenbuchung bei {recipient.name}" if recipient else ""
    return _redirect(
        f"Zahlung von {gb.name} erfasst: {format_euro(amount)}{info}", back
    )


@router.post(
    "/guest-booking/{gb_id}/unpaid", dependencies=[Depends(require_admin)]
)
async def admin_guest_unpaid(
    request: Request,
    gb_id: str,
    db: Session = Depends(get_db),
):
    gb = db.query(GuestBooking).filter(GuestBooking.id == gb_id).first()
    if not gb:
        return _redirect("Gastbuchung nicht gefunden", mt="error")
    back = f"/admin/event/{gb.event_id}"
    if not gb.paid_at:
        return _redirect("Nicht als bezahlt markiert", back, mt="error")
    services.unmark_guest_paid(db, gb)
    return _redirect(f"Bezahlt-Markierung von {gb.name} zurückgenommen", back)


# ── Statistics ────────────────────────────────────────────────────────────


@router.get(
    "/subscription/{sub_id}/stats", dependencies=[Depends(require_admin)]
)
async def subscription_stats(
    request: Request,
    sub_id: str,
    db: Session = Depends(get_db),
):
    sub = db.query(Subscription).filter(Subscription.id == sub_id).first()
    if not sub:
        return _redirect("Abo nicht gefunden", mt="error")

    members = (
        db.query(Member)
        .filter(Member.subscription_id == sub_id)
        .order_by(Member.name)
        .all()
    )
    events = (
        db.query(Event)
        .filter(Event.subscription_id == sub_id)
        .order_by(Event.date)
        .all()
    )

    member_ids = [m.id for m in members]
    bookings_by_member = dict(
        db.query(Booking.member_id, func.count(Booking.id))
        .filter(Booking.member_id.in_(member_ids))
        .group_by(Booking.member_id)
        .all()
    )
    spending = services.spending_by_member(db, member_ids)
    member_stats = []
    totals = {
        "deposited": Decimal("0.00"),
        "spent": Decimal("0.00"),
        "credit": Decimal("0.00"),
    }
    for m in members:
        member_stats.append(
            {
                "member": m,
                "bookings": bookings_by_member.get(m.id, 0),
                "deposited": spending[m.id]["deposited"],
                "spent": spending[m.id]["spent"],
                "credit": m.credit,
            }
        )
        totals["deposited"] += spending[m.id]["deposited"]
        totals["spent"] += spending[m.id]["spent"]
        totals["credit"] += m.credit

    event_ids = [e.id for e in events]
    booked = services.booked_counts(db, event_ids)
    revenue = {
        event_id: -Decimal(str(total)).quantize(Decimal("0.01"))
        for event_id, total in db.query(Payment.event_id, func.sum(Payment.amount))
        .filter(Payment.event_id.in_(event_ids), Payment.type == Payment.TYPE_CHARGE)
        .group_by(Payment.event_id)
    }
    event_stats = [
        {
            "event": e,
            "booked": booked[e.id],
            "revenue": revenue.get(e.id, Decimal("0.00")),
        }
        for e in events
    ]

    return TemplateResponse(
        "admin/stats.html",
        {
            "request": request,
            "subscription": sub,
            "member_stats": member_stats,
            "event_stats": event_stats,
            "totals": totals,
        },
    )
