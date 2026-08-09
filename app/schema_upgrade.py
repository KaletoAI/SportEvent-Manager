"""Idempotent in-place schema upgrades for existing SQLite databases.

`Base.metadata.create_all()` only creates missing tables; it never alters
existing ones. This module adds columns introduced after the initial schema.
Runs at every startup; each step is a no-op if the column already exists.
"""

import secrets

from sqlalchemy import Engine


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


def upgrade(engine: Engine) -> None:
    with engine.begin() as conn:
        # events: public guest-link token + settlement timestamp
        event_cols = _columns(conn, "events")
        if "public_token" not in event_cols:
            conn.exec_driver_sql(
                "ALTER TABLE events ADD COLUMN public_token VARCHAR(64)"
            )
            rows = conn.exec_driver_sql("SELECT id FROM events").fetchall()
            for (event_id,) in rows:
                conn.exec_driver_sql(
                    "UPDATE events SET public_token = ? WHERE id = ?",
                    (secrets.token_urlsafe(24), event_id),
                )
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_events_public_token "
                "ON events (public_token)"
            )
        if "settled_at" not in event_cols:
            conn.exec_driver_sql("ALTER TABLE events ADD COLUMN settled_at DATETIME")

        # events: frozen budgets + extra-event flag; backfill budgets by
        # spreading the subscription totals over its non-cancelled events
        if "abo_budget" not in event_cols:
            conn.exec_driver_sql(
                "ALTER TABLE events ADD COLUMN abo_budget NUMERIC(8,2) "
                "NOT NULL DEFAULT 0"
            )
            conn.exec_driver_sql(
                "ALTER TABLE events ADD COLUMN normal_budget NUMERIC(8,2) "
                "NOT NULL DEFAULT 0"
            )
            conn.exec_driver_sql(
                "ALTER TABLE events ADD COLUMN is_extra BOOLEAN "
                "NOT NULL DEFAULT 0"
            )
            subs = conn.exec_driver_sql(
                "SELECT id, abo_price, default_price FROM subscriptions"
            ).fetchall()
            for sub_id, abo_price, default_price in subs:
                (n,) = conn.exec_driver_sql(
                    "SELECT COUNT(*) FROM events "
                    "WHERE subscription_id = ? AND is_cancelled = 0",
                    (sub_id,),
                ).fetchone()
                if not n:
                    continue
                # * 1.0 forces REAL division (SQLite would divide integers)
                conn.exec_driver_sql(
                    "UPDATE events SET abo_budget = ROUND(? * 1.0 / ?, 2), "
                    "normal_budget = ROUND(? * 1.0 / ?, 2) "
                    "WHERE subscription_id = ? AND is_cancelled = 0",
                    (abo_price, n, default_price, n, sub_id),
                )

        # subscriptions + events: minimum participants for events to happen
        sub_cols = _columns(conn, "subscriptions")
        if "min_participants" not in sub_cols:
            conn.exec_driver_sql(
                "ALTER TABLE subscriptions ADD COLUMN min_participants INTEGER "
                "NOT NULL DEFAULT 4"
            )
        if "min_participants" not in event_cols:
            conn.exec_driver_sql(
                "ALTER TABLE events ADD COLUMN min_participants INTEGER "
                "NOT NULL DEFAULT 4"
            )
            conn.exec_driver_sql(
                "UPDATE events SET min_participants = ("
                "SELECT min_participants FROM subscriptions "
                "WHERE subscriptions.id = events.subscription_id)"
            )

        # members: super-member flag
        member_cols = _columns(conn, "members")
        if "is_super" not in member_cols:
            conn.exec_driver_sql(
                "ALTER TABLE members ADD COLUMN is_super BOOLEAN "
                "NOT NULL DEFAULT 0"
            )

        # subscriptions: cancellation deadlines in hours (formerly days)
        if "cancel_hours_free" not in sub_cols:
            conn.exec_driver_sql(
                "ALTER TABLE subscriptions ADD COLUMN cancel_hours_free INTEGER "
                "NOT NULL DEFAULT 48"
            )
            conn.exec_driver_sql(
                "ALTER TABLE subscriptions ADD COLUMN cancel_hours_approval "
                "INTEGER NOT NULL DEFAULT 0"
            )
            if "cancel_days_free" in sub_cols:
                # Übernahme aus der kurzlebigen Tage-Variante
                conn.exec_driver_sql(
                    "UPDATE subscriptions SET "
                    "cancel_hours_free = cancel_days_free * 24, "
                    "cancel_hours_approval = cancel_days_approval * 24"
                )

        # events: reminder flag; bookings: cancellation request
        if "reminder_sent" not in event_cols:
            conn.exec_driver_sql(
                "ALTER TABLE events ADD COLUMN reminder_sent BOOLEAN "
                "NOT NULL DEFAULT 0"
            )
        booking_cols = _columns(conn, "bookings")
        if "cancel_requested_at" not in booking_cols:
            conn.exec_driver_sql(
                "ALTER TABLE bookings ADD COLUMN cancel_requested_at DATETIME"
            )

        # payout mode + member paypal (Vorstreck-Modell)
        if "payout_mode" not in sub_cols:
            conn.exec_driver_sql(
                "ALTER TABLE subscriptions ADD COLUMN payout_mode VARCHAR(10) "
                "NOT NULL DEFAULT 'central'"
            )
        if "paypal_address" not in member_cols:
            conn.exec_driver_sql(
                "ALTER TABLE members ADD COLUMN paypal_address VARCHAR(200) "
                "DEFAULT ''"
            )

        # login_tokens: 6-digit code (table may predate the column)
        lt_cols = _columns(conn, "login_tokens")
        if lt_cols and "code" not in lt_cols:
            conn.exec_driver_sql(
                "ALTER TABLE login_tokens ADD COLUMN code VARCHAR(6) "
                "NOT NULL DEFAULT ''"
            )

        # persons directory: one-time backfill from existing members
        (persons_count,) = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM persons"
        ).fetchone()
        if persons_count == 0:
            rows = conn.exec_driver_sql(
                "SELECT email, name, COALESCE(paypal_address, '') "
                "FROM members ORDER BY created_at"
            ).fetchall()
            latest = {email: (name, paypal) for email, name, paypal in rows}
            import uuid as _uuid_mod

            for email, (name, paypal) in latest.items():
                conn.exec_driver_sql(
                    "INSERT INTO persons (id, email, name, paypal_address, "
                    "created_at) VALUES (?, ?, ?, ?, datetime('now'))",
                    (_uuid_mod.uuid4().hex[:12], email, name, paypal),
                )

        # guest_bookings: "Bezahlt"-Tracking mit Gegenbuchung
        gb_cols = _columns(conn, "guest_bookings")
        if gb_cols and "paid_at" not in gb_cols:
            conn.exec_driver_sql(
                "ALTER TABLE guest_bookings ADD COLUMN paid_at DATETIME"
            )
        if gb_cols and "paid_amount" not in gb_cols:
            conn.exec_driver_sql(
                "ALTER TABLE guest_bookings ADD COLUMN paid_amount DECIMAL(10, 2)"
            )
        if gb_cols and "paid_member_id" not in gb_cols:
            conn.exec_driver_sql(
                "ALTER TABLE guest_bookings ADD COLUMN paid_member_id "
                "VARCHAR(12) REFERENCES members(id)"
            )

        # payments: ledger type + optional event reference
        payment_cols = _columns(conn, "payments")
        if "type" not in payment_cols:
            conn.exec_driver_sql(
                "ALTER TABLE payments ADD COLUMN type VARCHAR(20) "
                "NOT NULL DEFAULT 'deposit'"
            )
        if "event_id" not in payment_cols:
            conn.exec_driver_sql(
                "ALTER TABLE payments ADD COLUMN event_id VARCHAR(12) "
                "REFERENCES events(id)"
            )
