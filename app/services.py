"""Shared domain logic: capacity, budgets, price tiers, settlement.

Pricing model (cost sharing with frozen budgets):
- Subscription.abo_price / default_price are TOTALS for the Abo period.
- Every regular event carries frozen budgets (abo_budget / normal_budget).
  `recompute_budgets` spreads the totals over the OPEN (not settled, not
  cancelled, not extra) events; settled events keep their budget.
- Extra events (is_extra) have their own budget outside the totals;
  members and guests pay the same share there.
- At an event, members split abo_budget, guests pay the normal_budget
  share — both divided by ALL participants of that event.
- Events take place only at min_participants or more; settlement below
  the minimum is refused (cancel the event instead).
"""

from datetime import date, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.emailer import (
    guest_settlement_email_body,
    send_email,
    settlement_email_body,
    waitlist_promoted_email_body,
)
from app.models.models import (
    Booking,
    Event,
    GuestBooking,
    Member,
    Payment,
    Person,
    Subscription,
    WaitlistEntry,
    utcnow,
)


# ── Capacity ───────────────────────────────────────────────────────────────


def count_booked(db: Session, event_id: str) -> int:
    """Total occupied spots: members (1 + their guests) plus link guests."""
    member_spots = (
        db.query(func.coalesce(func.sum(Booking.guest_count + 1), 0))
        .filter(Booking.event_id == event_id)
        .scalar()
    )
    guest_spots = (
        db.query(func.coalesce(func.sum(GuestBooking.count), 0))
        .filter(GuestBooking.event_id == event_id)
        .scalar()
    )
    return int(member_spots) + int(guest_spots)


def free_spots(db: Session, event: Event) -> int:
    return max(0, event.max_participants - count_booked(db, event.id))


# ── Budgets ────────────────────────────────────────────────────────────────


def _cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def recompute_budgets(db: Session, subscription: Subscription) -> None:
    """Spread the Abo totals over the open regular events.

    Settled events keep their frozen budget; whatever remains of the
    totals is divided evenly among the open events. Extra events are
    never touched. Caller commits.
    """
    regular = [
        e
        for e in subscription.events
        if not e.is_extra and not e.is_cancelled
    ]
    settled = [e for e in regular if e.settled_at]
    open_events = [e for e in regular if not e.settled_at]
    if not open_events:
        return
    abo_rest = subscription.abo_price - sum(
        (e.abo_budget for e in settled), Decimal("0.00")
    )
    normal_rest = subscription.default_price - sum(
        (e.normal_budget for e in settled), Decimal("0.00")
    )
    n = len(open_events)
    for e in open_events:
        e.abo_budget = _cents(max(abo_rest, Decimal("0")) / n)
        e.normal_budget = _cents(max(normal_rest, Decimal("0")) / n)


def cancel_event(db: Session, event: Event, reduce_price: bool) -> None:
    """Cancel a regular event, answering the price question:

    reduce_price=True  → the Abo totals shrink by this event's budgets
                         (remaining budgets stay unchanged).
    reduce_price=False → totals stay, the budget is redistributed over
                         the remaining open events (they get pricier).

    Extra events just get cancelled (their budget is outside the totals).
    """
    event.is_cancelled = True
    if not event.is_extra:
        if reduce_price:
            sub = event.subscription
            sub.abo_price = max(Decimal("0.00"), sub.abo_price - event.abo_budget)
            sub.default_price = max(
                Decimal("0.00"), sub.default_price - event.normal_budget
            )
        recompute_budgets(db, event.subscription)
    db.commit()


def reactivate_event(db: Session, event: Event) -> None:
    """Undo a cancellation. Note: a price reduction chosen at cancel time
    is NOT restored automatically — adjust the Abo prices if needed."""
    event.is_cancelled = False
    if not event.is_extra:
        recompute_budgets(db, event.subscription)
    db.commit()


def create_extra_event(
    db: Session,
    subscription: Subscription,
    event_date: date,
    start_time: time,
    duration_minutes: int,
    budget: Decimal,
    max_participants: int,
    min_participants: int,
) -> Event:
    """Extra event outside the Abo totals: everyone pays the same share
    of its own budget. Raises IntegrityError if the date is taken."""
    from datetime import datetime

    end_dt = datetime.combine(event_date, start_time) + timedelta(
        minutes=duration_minutes
    )
    event = Event(
        subscription_id=subscription.id,
        date=event_date,
        start_time=start_time,
        end_time=end_dt.time(),
        max_participants=max_participants,
        min_participants=min_participants,
        abo_budget=budget,
        normal_budget=budget,
        is_extra=True,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


# ── Waitlist ───────────────────────────────────────────────────────────────


def waitlist_entries(db: Session, event_id: str) -> list[WaitlistEntry]:
    return (
        db.query(WaitlistEntry)
        .filter(WaitlistEntry.event_id == event_id)
        .order_by(WaitlistEntry.created_at)
        .all()
    )


async def promote_from_waitlist(db: Session, event: Event) -> list[Member]:
    """Fill freed spots from the waitlist (FIFO): the first entries become
    bookings (without guests) and get notified by mail. Call after
    anything that frees spots. Returns the promoted members."""
    from app import clock

    if event.is_cancelled or event.settled_at or event.date < clock.today(db):
        return []
    promoted = []
    while free_spots(db, event) > 0:
        entry = (
            db.query(WaitlistEntry)
            .filter(WaitlistEntry.event_id == event.id)
            .order_by(WaitlistEntry.created_at)
            .first()
        )
        if not entry:
            break
        member = entry.member
        db.add(Booking(event_id=event.id, member_id=member.id, guest_count=0))
        db.delete(entry)
        db.commit()
        promoted.append(member)
        await send_email(
            event.subscription,
            member.email,
            f"Nachgerückt: {event.date.strftime('%d.%m.%Y')} – "
            f"{event.subscription.name}",
            waitlist_promoted_email_body(
                member.name,
                event.date.strftime("%d.%m.%Y"),
                event.start_time.strftime("%H:%M"),
            ),
        )
    return promoted


# ── Prices & tiers ─────────────────────────────────────────────────────────


def per_person_share(total: Decimal, participants: int) -> Decimal:
    """Cost share per person when `participants` people split `total`."""
    if participants <= 0:
        return Decimal("0.00")
    return _cents(total / participants)


def event_shares(db: Session, event: Event) -> dict:
    """Per-person prices based on the event's current participants.
    Final at settlement; before that they are estimates."""
    participants = count_booked(db, event.id)
    return {
        "participants": participants,
        "member_share": per_person_share(event.abo_budget, participants),
        "guest_share": per_person_share(event.normal_budget, participants),
    }


def price_tiers(event: Event, limit: Optional[int] = None) -> list[dict]:
    """Price ladder from min_participants up to max_participants:
    [{'n': 4, 'member': …, 'guest': …}, {'n': 5, …}, …]"""
    start = max(1, event.min_participants)
    end = max(start, event.max_participants)
    tiers = [
        {
            "n": n,
            "member": per_person_share(event.abo_budget, n),
            "guest": per_person_share(event.normal_budget, n),
        }
        for n in range(start, end + 1)
    ]
    return tiers[:limit] if limit else tiers


# ── Settlement ─────────────────────────────────────────────────────────────


def settle_event(db: Session, event: Event) -> tuple[int, Decimal, dict]:
    """Charge every member booking of the event against their credit.

    Members pay their share of abo_budget, their guests the normal_budget
    share. Link guests are not charged here (no account) — the admin
    collects their share via PayPal (shown in the UI).

    Idempotent via `event.settled_at`. Returns (charged, total, shares).
    Caller must ensure the event is settleable (not cancelled, not
    settled, min_participants reached).
    """
    shares = event_shares(db, event)
    total = Decimal("0.00")
    charged = 0
    for booking in event.bookings:
        amount = shares["member_share"] + booking.guest_count * shares["guest_share"]
        note = f"Teilnahme {event.date.strftime('%d.%m.%Y')}"
        if event.is_extra:
            note = f"Zusatztermin {event.date.strftime('%d.%m.%Y')}"
        if booking.guest_count:
            note += f" (+{booking.guest_count} Gäste)"
        payment = Payment(
            member_id=booking.member_id,
            amount=-amount,
            type=Payment.TYPE_CHARGE,
            event_id=event.id,
            note=note,
        )
        booking.member.credit -= amount
        db.add(payment)
        total += amount
        charged += 1
    event.settled_at = utcnow()
    db.commit()
    return charged, total, shares


async def settle_and_notify(db: Session, event: Event) -> tuple[int, Decimal, int]:
    """Settle the event, then email each charged member (best effort;
    skipped without SMTP config). Returns (charged, total, emails sent)."""
    from app.templates import format_date, format_euro

    charged, total, shares = settle_event(db, event)
    payee = payee_info(db, event.subscription)
    sent = 0
    for booking in event.bookings:
        member = booking.member
        amount = shares["member_share"] + booking.guest_count * shares["guest_share"]
        ok = await send_email(
            event.subscription,
            member.email,
            f"Abrechnung {format_date(event.date)} – {event.subscription.name}",
            settlement_email_body(
                member.name,
                event.date.strftime("%d.%m.%Y"),
                format_euro(amount),
                format_euro(member.credit),
                payee["paypal"],
                payee["name"],
                is_payee=(payee["member_id"] == member.id),
            ),
        )
        sent += 1 if ok else 0
    # Link-Gäste: kein Ledger, aber Abrechnungsmail mit Zahlungsziel —
    # bereits als bezahlt markierte Gäste bekommen keine Zahlungsaufforderung.
    guest_bookings = (
        db.query(GuestBooking).filter(GuestBooking.event_id == event.id).all()
    )
    for gb in guest_bookings:
        if not gb.email or gb.paid_at:
            continue
        ok = await send_email(
            event.subscription,
            gb.email,
            f"Abrechnung {format_date(event.date)} – {event.subscription.name}",
            guest_settlement_email_body(
                gb.name,
                event.date.strftime("%d.%m.%Y"),
                gb.count,
                format_euro(gb.count * shares["guest_share"]),
                payee["paypal"],
                payee["name"],
            ),
        )
        sent += 1 if ok else 0
    if sent:
        event.payment_sent = True
        db.commit()
    return charged, total, sent


def settle_blocker(db: Session, event: Event) -> Optional[str]:
    """Why this event cannot be settled right now, or None if it can."""
    from app import clock

    if event.is_cancelled:
        return "Abgesagte Termine werden nicht abgerechnet"
    if event.settled_at:
        return "Termin ist bereits abgerechnet"
    if event.date > clock.today(db):
        return "Termin liegt in der Zukunft"
    participants = count_booked(db, event.id)
    if participants < event.min_participants:
        return (
            f"Mindestteilnehmerzahl nicht erreicht "
            f"({participants} von {event.min_participants}) – bitte Termin absagen"
        )
    return None


def delete_subscription(db: Session, subscription: Subscription) -> None:
    """Delete an Abo with EVERYTHING attached to it: events, bookings,
    guest bookings, members, their ledgers, sessions and login tokens.
    Irreversible — the caller must confirm with the user. Entries in the
    central person directory are kept."""
    from app.models.models import LoginToken, UserSession

    event_ids = [e.id for e in subscription.events]
    member_ids = [m.id for m in subscription.members]
    if event_ids:
        db.query(GuestBooking).filter(
            GuestBooking.event_id.in_(event_ids)
        ).delete(synchronize_session=False)
        db.query(Booking).filter(Booking.event_id.in_(event_ids)).delete(
            synchronize_session=False
        )
    if member_ids:
        db.query(Payment).filter(Payment.member_id.in_(member_ids)).delete(
            synchronize_session=False
        )
        db.query(LoginToken).filter(
            LoginToken.member_id.in_(member_ids)
        ).delete(synchronize_session=False)
        db.query(UserSession).filter(
            UserSession.member_id.in_(member_ids)
        ).delete(synchronize_session=False)
    db.query(Event).filter(Event.subscription_id == subscription.id).delete(
        synchronize_session=False
    )
    db.query(Member).filter(Member.subscription_id == subscription.id).delete(
        synchronize_session=False
    )
    db.delete(subscription)
    db.commit()


# ── Central user directory ─────────────────────────────────────────────────


def upsert_person(
    db: Session, name: str, email: str, paypal_address: str = ""
) -> Person:
    """Keep the central directory in sync (one entry per email).
    Caller commits."""
    person = db.query(Person).filter(Person.email == email).first()
    if person:
        person.name = name
        if paypal_address:
            person.paypal_address = paypal_address
    else:
        person = Person(
            email=email, name=name, paypal_address=paypal_address or ""
        )
        db.add(person)
    return person


# ── Payout target ──────────────────────────────────────────────────────────


def payee_info(db: Session, subscription: Subscription) -> dict:
    """Where payments should go.

    payout_mode == "member": the active member with the highest credit
    and a PayPal address (the one who fronted the money). Paying them
    is recorded as a transfer (payer +, payee −). Falls back to the
    central address when no member qualifies.
    """
    if subscription.payout_mode == "member":
        member = (
            db.query(Member)
            .filter(
                Member.subscription_id == subscription.id,
                Member.is_active == True,  # noqa: E712
                Member.paypal_address != "",
                Member.paypal_address.isnot(None),
            )
            .order_by(Member.credit.desc())
            .first()
        )
        if member:
            return {
                "paypal": member.paypal_address,
                "name": member.name,
                "member_id": member.id,
            }
    return {
        "paypal": subscription.paypal_address or "",
        "name": None,
        "member_id": None,
    }


def record_transfer(
    db: Session, payer: Member, payee: Member, amount: Decimal, note: str = ""
) -> None:
    """Record a real-money payment between members (Vorstreck-Modell):
    the payer's balance rises, the payee's falls — the payee got their
    fronted money back. Caller validates amount > 0 and same subscription."""
    suffix = f" ({note})" if note else ""
    db.add(
        Payment(
            member_id=payer.id,
            amount=amount,
            type=Payment.TYPE_DEPOSIT,
            note=f"Zahlung an {payee.name}{suffix}",
        )
    )
    db.add(
        Payment(
            member_id=payee.id,
            amount=-amount,
            type=Payment.TYPE_DEPOSIT,
            note=f"Erhalten von {payer.name}{suffix}",
        )
    )
    payer.credit += amount
    payee.credit -= amount
    db.commit()


def mark_guest_paid(
    db: Session, gb: GuestBooking, amount: Decimal
) -> Optional[Member]:
    """Gastzahlung als erhalten markieren. Hat ein Mitglied das Geld
    vorgestreckt (payout_mode 'member'), sinkt dessen Guthaben um den
    Betrag (Gegenbuchung) — bei zentraler Kasse wird nur der Status
    gespeichert. Caller validates amount > 0 and not yet paid.
    Returns the credited member, if any."""
    payee = payee_info(db, gb.event.subscription)
    recipient = None
    if payee["member_id"]:
        recipient = db.get(Member, payee["member_id"])
        db.add(
            Payment(
                member_id=recipient.id,
                amount=-amount,
                type=Payment.TYPE_DEPOSIT,
                event_id=gb.event_id,
                note=f"Gastzahlung von {gb.name} erhalten",
            )
        )
        recipient.credit -= amount
    gb.paid_at = utcnow()
    gb.paid_amount = amount
    gb.paid_member_id = recipient.id if recipient else None
    db.commit()
    return recipient


def unmark_guest_paid(db: Session, gb: GuestBooking) -> None:
    """Bezahlt-Markierung stornieren; eine Gegenbuchung wird rückgebucht."""
    if gb.paid_member_id and gb.paid_amount:
        recipient = db.get(Member, gb.paid_member_id)
        if recipient:
            db.add(
                Payment(
                    member_id=recipient.id,
                    amount=gb.paid_amount,
                    type=Payment.TYPE_DEPOSIT,
                    event_id=gb.event_id,
                    note=f"Storno Gastzahlung von {gb.name}",
                )
            )
            recipient.credit += gb.paid_amount
    gb.paid_at = None
    gb.paid_amount = None
    gb.paid_member_id = None
    db.commit()


# ── Stats ──────────────────────────────────────────────────────────────────


def member_spending(db: Session, member_id: str) -> dict:
    """Ledger aggregates for one member."""
    rows = (
        db.query(Payment.type, func.coalesce(func.sum(Payment.amount), 0))
        .filter(Payment.member_id == member_id)
        .group_by(Payment.type)
        .all()
    )
    by_type = {t: Decimal(str(s)) for t, s in rows}
    deposited = by_type.get(Payment.TYPE_DEPOSIT, Decimal("0.00"))
    charged = -by_type.get(Payment.TYPE_CHARGE, Decimal("0.00"))
    return {"deposited": deposited, "spent": charged}
