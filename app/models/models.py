"""SQLAlchemy models for SportAbo Manager."""

import secrets
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    DECIMAL,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


def _public_token() -> str:
    return secrets.token_urlsafe(24)


def utcnow() -> datetime:
    """Naive UTC timestamp (consistent with existing DB rows)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Subscription(Base):
    """An Abo: e.g. 'Beachvolleyball Summer 2025'."""

    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, default="")

    # Event generation rules
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)  # 0=Mon .. 6=Sun
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=120)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    # Pricing — both are TOTALS for the whole subscription period.
    # Per-event budget = total / active event count; each event's budget
    # is split among its participants: members share the abo budget,
    # guests pay the normal-price share (see services.event_shares).
    default_price: Mapped[Decimal] = mapped_column(
        DECIMAL(8, 2), nullable=False, default=10.00
    )
    abo_price: Mapped[Decimal] = mapped_column(
        DECIMAL(8, 2), nullable=False, default=8.00
    )

    # Capacity: events need min_participants to take place
    max_participants: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    min_participants: Mapped[int] = mapped_column(Integer, nullable=False, default=4)

    # Cancellation deadlines (hours before the event START time):
    # until cancel_hours_free hours before → members cancel freely;
    # until cancel_hours_approval hours before → only with super approval;
    # after that → no cancellation.
    cancel_hours_free: Mapped[int] = mapped_column(
        Integer, nullable=False, default=48
    )
    cancel_hours_approval: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    # Deprecated: guests now pay a share of default_price (kept only so
    # existing DB rows keep loading; not used in any pricing logic).
    guest_price_default: Mapped[Decimal] = mapped_column(
        DECIMAL(8, 2), nullable=False, default=10.00
    )

    # PayPal (central account, used when payout_mode == "central")
    paypal_address: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True, default=""
    )
    # Where payments go: "central" = the Abo's PayPal address,
    # "member" = the active member with the highest credit (who fronted
    # the money) and a PayPal address.
    payout_mode: Mapped[str] = mapped_column(
        String(10), nullable=False, default="central"
    )

    # Email sender config
    email_sender: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True, default=""
    )
    email_from_name: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, default=""
    )
    smtp_host: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True, default=""
    )
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    smtp_user: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True, default=""
    )
    smtp_password: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True, default=""
    )
    smtp_use_tls: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    events: Mapped[list["Event"]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan"
    )
    members: Mapped[list["Member"]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan"
    )


class Person(Base):
    """Central user directory (one row per email), reusable across Abos.

    Kept in sync whenever members are created/edited; adding a member to
    a subscription can copy from here instead of retyping the data.
    """

    __tablename__ = "persons"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    paypal_address: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True, default=""
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Member(Base):
    """A member of a subscription."""

    __tablename__ = "members"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    credit: Mapped[Decimal] = mapped_column(
        DECIMAL(10, 2), nullable=False, default=0.00
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Super-Member: may cancel events (with price question), create extra
    # events, settle events and see participant lists.
    is_super: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Payment target when this member fronted the Abo money
    # (payout_mode == "member")
    paypal_address: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True, default=""
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    subscription: Mapped["Subscription"] = relationship(back_populates="members")
    bookings: Mapped[list["Booking"]] = relationship(
        back_populates="member", cascade="all, delete-orphan"
    )
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="member", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("subscription_id", "email", name="uq_member_email_per_abo"),
    )


class Event(Base):
    """A single event/date of a subscription."""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)

    # Secret token for the public guest booking link (/g/{public_token}).
    # Separate from the id so admin URLs don't leak the public link.
    public_token: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, default=_public_token
    )

    max_participants: Mapped[int] = mapped_column(Integer, nullable=False)
    # Copied from the subscription at creation; adjustable per event.
    # The event only takes place (and can only be settled) at or above it.
    min_participants: Mapped[int] = mapped_column(Integer, nullable=False, default=4)

    # Frozen budgets (assigned at generation, redistributed only over
    # open events — see services.recompute_budgets). Members split
    # abo_budget, guests split normal_budget among all participants.
    abo_budget: Mapped[Decimal] = mapped_column(
        DECIMAL(8, 2), nullable=False, default=0.00
    )
    normal_budget: Mapped[Decimal] = mapped_column(
        DECIMAL(8, 2), nullable=False, default=0.00
    )
    # Extra event outside the Abo totals: own budget (abo_budget ==
    # normal_budget == entered price), everyone pays the same share.
    is_extra: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Deprecated: fixed per-event guest price, superseded by cost sharing.
    guest_price: Mapped[Optional[Decimal]] = mapped_column(
        DECIMAL(8, 2), nullable=True
    )

    # Status
    is_cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    payment_sent: Mapped[bool] = mapped_column(
        Boolean, default=False
    )  # invoice email sent
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Scheduler: reminder mail (1 day before last free cancellation) sent
    reminder_sent: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    subscription: Mapped["Subscription"] = relationship(back_populates="events")
    bookings: Mapped[list["Booking"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "subscription_id", "date", name="uq_event_per_subscription_date"
        ),
    )


class Booking(Base):
    """A booking of a member for an event, possibly with guests."""

    __tablename__ = "bookings"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(
        ForeignKey("events.id"), nullable=False
    )
    member_id: Mapped[str] = mapped_column(
        ForeignKey("members.id"), nullable=False
    )
    guest_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    guest_emails: Mapped[Optional[str]] = mapped_column(
        Text, default=""
    )  # comma-separated
    # Set when the member asked to cancel inside the approval window;
    # a super member approves (booking deleted) or rejects (cleared).
    cancel_requested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    event: Mapped["Event"] = relationship(back_populates="bookings")
    member: Mapped["Member"] = relationship(back_populates="bookings")

    __table_args__ = (
        UniqueConstraint("event_id", "member_id", name="uq_booking_per_event_member"),
    )


class Payment(Base):
    """Ledger entry for a member: deposits (+) and charges (−).

    `Member.credit` is the cached sum of all entries; every write to the
    ledger must adjust it in the same transaction.
    """

    __tablename__ = "payments"

    TYPE_DEPOSIT = "deposit"
    TYPE_CHARGE = "charge"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    member_id: Mapped[str] = mapped_column(
        ForeignKey("members.id"), nullable=False
    )
    # Signed: deposits positive, charges negative.
    amount: Mapped[Decimal] = mapped_column(DECIMAL(10, 2), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False, default=TYPE_DEPOSIT)
    event_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("events.id"), nullable=True
    )
    note: Mapped[Optional[str]] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    member: Mapped["Member"] = relationship(back_populates="payments")
    event: Mapped[Optional["Event"]] = relationship()


class GuestBooking(Base):
    """A guest booking via public link (no member account)."""

    __tablename__ = "guest_bookings"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(
        ForeignKey("events.id"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    token: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, default=lambda: uuid.uuid4().hex
    )
    # "Bezahlt"-Tracking: wann/wieviel der Gast gezahlt hat und wessen
    # Guthaben die Gegenbuchung senkte (None = zentrale Kasse, kein Ledger).
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    paid_amount: Mapped[Optional[Decimal]] = mapped_column(
        DECIMAL(10, 2), nullable=True
    )
    paid_member_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("members.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    event: Mapped["Event"] = relationship()


class WaitlistEntry(Base):
    """FIFO waitlist for full events: when a spot frees up, the first
    entry is auto-promoted to a booking and notified by mail."""

    __tablename__ = "waitlist"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), nullable=False)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    event: Mapped["Event"] = relationship()
    member: Mapped["Member"] = relationship()

    __table_args__ = (
        UniqueConstraint("event_id", "member_id", name="uq_waitlist_event_member"),
    )


class LoginToken(Base):
    """One-time login token for passwordless member login via email."""

    __tablename__ = "login_tokens"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    token: Mapped[str] = mapped_column(
        String(96), nullable=False, unique=True, index=True
    )
    # 6-digit alternative for manual entry (same lifetime as the link)
    code: Mapped[str] = mapped_column(String(6), nullable=False, default="")
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    member: Mapped["Member"] = relationship()


class AppSetting(Base):
    """Simple key-value store for runtime settings (e.g. date override)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)


class UserSession(Base):
    """Server-side session for members and the admin (revocable, expiring)."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    token: Mapped[str] = mapped_column(
        String(96), nullable=False, unique=True, index=True
    )
    member_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("members.id"), nullable=True
    )
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    member: Mapped[Optional["Member"]] = relationship()
