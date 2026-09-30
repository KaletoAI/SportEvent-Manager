"""Link-Gäste über der Mindestzahl brauchen die Bestätigung eines
Super-(„Plus“-)Mitglieds: bis zur Mindestzahl sofort bestätigt, darüber
eine Anfrage, die den Platz reserviert."""

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from app import scheduler, services
from app.models.models import Base, Booking, Event, GuestBooking, WaitlistEntry
from app.schema_upgrade import upgrade
from tests.conftest import admin_login, get_csrf, member_login


@pytest.fixture
def outbox(monkeypatch):
    mails = []

    async def fake_send_batch(subscription, batch):
        mails.extend({"to": m.to, "subject": m.subject, "body": m.body} for m in batch)
        return len(batch)

    monkeypatch.setattr(services, "send_batch", fake_send_batch)
    monkeypatch.setattr(scheduler, "send_batch", fake_send_batch)
    return mails


@pytest.fixture
def ev(db, seed):
    """Termin in 3 Tagen: min 2, max 4."""
    event = seed["event"]
    event.min_participants = 2
    event.max_participants = 4
    db.commit()
    return event


def _book(client, event, name="Gast", email="gast@example.com", count=1):
    csrf = get_csrf(client, f"/g/{event.public_token}")
    return client.post(
        f"/g/{event.public_token}/book",
        data={"name": name, "email": email, "count": count, "csrf_token": csrf},
        follow_redirects=False,
    )


def _gb(db, email="gast@example.com"):
    db.expire_all()
    return db.query(GuestBooking).filter(GuestBooking.email == email).first()


def _add_member_booking(db, seed, event, guests=0):
    db.add(Booking(event_id=event.id, member_id=seed["member"].id, guest_count=guests))
    db.commit()


# ── Buchen ────────────────────────────────────────────────────────────────


def test_up_to_minimum_is_confirmed_immediately(client, db, seed, ev, outbox):
    _add_member_booking(db, seed, ev)  # 1 belegt
    resp = _book(client, ev)  # → 2 = Mindestzahl
    assert resp.status_code == 302
    gb = _gb(db)
    assert gb.confirmed_at is not None
    assert outbox == []
    page = client.get(resp.headers["location"])
    assert "ist bestätigt" in page.text


def test_above_minimum_becomes_request(client, db, seed, ev, outbox):
    _add_member_booking(db, seed, ev, guests=1)  # 2 belegt = Mindestzahl
    resp = _book(client, ev)  # → 3
    gb = _gb(db)
    assert gb.confirmed_at is None
    # Platz ist reserviert
    assert services.count_booked(db, ev.id) == 3
    # Super-Mitglied Sina wird informiert, normale Mitglieder nicht
    assert [m["to"] for m in outbox] == ["sina@example.com"]
    assert "Gast" in outbox[0]["body"]
    page = client.get(resp.headers["location"])
    assert "noch von einem Plus-Mitglied bestätigt" in page.text
    assert "ist bestätigt" not in page.text


def test_booking_straddling_minimum_is_request(client, db, seed, ev, outbox):
    _add_member_booking(db, seed, ev)  # 1 belegt
    _book(client, ev, count=2)  # → 3, reicht über die Mindestzahl 2
    assert _gb(db).confirmed_at is None


def test_guest_page_hints_at_approval_when_minimum_reached(client, db, seed, ev):
    _add_member_booking(db, seed, ev, guests=1)
    page = client.get(f"/g/{ev.public_token}")
    assert "bestätigt werden" in page.text


def test_admin_direct_guest_booking_needs_no_approval(client, db, seed, ev):
    _add_member_booking(db, seed, ev, guests=1)
    csrf = admin_login(client)
    client.post(
        f"/admin/event/{ev.id}/book-guest",
        data={"name": "Direkt", "email": "direkt@example.com", "count": 1,
              "csrf_token": csrf},
        follow_redirects=False,
    )
    assert _gb(db, "direkt@example.com").confirmed_at is not None


# ── Bestätigen / Ablehnen ─────────────────────────────────────────────────


def _pending(client, db, seed, ev, outbox):
    _add_member_booking(db, seed, ev, guests=1)
    resp = _book(client, ev)
    outbox.clear()
    return _gb(db), resp.headers["location"]


def test_super_confirms_request(client, db, seed, ev, outbox):
    gb, confirm_url = _pending(client, db, seed, ev, outbox)
    csrf = member_login(client, "sina@example.com")
    dash = client.get("/member/dashboard")
    assert "Gast-Anfragen" in dash.text
    assert "Verwaltung (1)" in dash.text
    assert f"/member/guest-booking/{gb.id}/confirm" in dash.text
    resp = client.post(
        f"/member/guest-booking/{gb.id}/confirm",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert _gb(db).confirmed_at is not None
    assert [m["to"] for m in outbox] == ["gast@example.com"]
    assert "bestätigt" in outbox[0]["subject"]
    assert "pay@example.com" in outbox[0]["body"]
    assert "ist bestätigt" in client.get(confirm_url).text


def test_confirm_twice_is_refused(client, db, seed, ev, outbox):
    gb, _ = _pending(client, db, seed, ev, outbox)
    csrf = member_login(client, "sina@example.com")
    for _ in range(2):
        client.post(f"/member/guest-booking/{gb.id}/confirm",
                    data={"csrf_token": csrf}, follow_redirects=False)
    assert len(outbox) == 1


def test_super_rejects_request_and_waitlist_moves_up(client, db, seed, ev, outbox):
    gb, _ = _pending(client, db, seed, ev, outbox)
    # Termin voll machen, dann steht Sina auf der Warteliste
    db.add(GuestBooking(event_id=ev.id, name="Voll", email="voll@example.com", count=1))
    db.add(WaitlistEntry(event_id=ev.id, member_id=seed["super_member"].id))
    db.commit()
    csrf = member_login(client, "sina@example.com")
    resp = client.post(
        f"/member/guest-booking/{gb.id}/reject",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert _gb(db) is None
    recipients = [m["to"] for m in outbox]
    assert "gast@example.com" in recipients
    assert "sina@example.com" in recipients  # nachgerückt
    rejection = next(m for m in outbox if m["to"] == "gast@example.com")
    assert "nicht" in rejection["body"]


def test_regular_member_cannot_confirm(client, db, seed, ev, outbox):
    gb, _ = _pending(client, db, seed, ev, outbox)
    csrf = member_login(client, "anna@example.com")
    resp = client.post(f"/member/guest-booking/{gb.id}/confirm",
                       data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 403
    assert _gb(db).confirmed_at is None


def test_super_of_other_abo_cannot_confirm(client, db, seed, ev, outbox):
    gb, _ = _pending(client, db, seed, ev, outbox)
    seed["outsider"].is_super = True
    db.commit()
    csrf = member_login(client, "bernd@example.com")
    for action in ("confirm", "reject"):
        client.post(f"/member/guest-booking/{gb.id}/{action}",
                    data={"csrf_token": csrf}, follow_redirects=False)
    gb = _gb(db)
    assert gb is not None and gb.confirmed_at is None
    assert outbox == []


def test_admin_confirms_and_rejects(client, db, seed, ev, outbox):
    gb, _ = _pending(client, db, seed, ev, outbox)
    csrf = admin_login(client)
    page = client.get(f"/admin/event/{ev.id}")
    assert f"/admin/guest-booking/{gb.id}/confirm" in page.text
    client.post(f"/admin/guest-booking/{gb.id}/confirm",
                data={"csrf_token": csrf}, follow_redirects=False)
    assert _gb(db).confirmed_at is not None

    _book(client, ev, email="zwei@example.com")
    gb2 = _gb(db, "zwei@example.com")
    assert gb2.confirmed_at is None
    client.post(f"/admin/guest-booking/{gb2.id}/reject",
                data={"csrf_token": csrf}, follow_redirects=False)
    assert _gb(db, "zwei@example.com") is None


def test_participants_page_shows_request_buttons_for_super(client, db, seed, ev, outbox):
    gb, _ = _pending(client, db, seed, ev, outbox)
    member_login(client, "sina@example.com")
    page = client.get(f"/member/event/{ev.id}/participants")
    assert f"/member/guest-booking/{gb.id}/confirm" in page.text
    assert f"/member/guest-booking/{gb.id}/reject" in page.text


# ── Abrechnung / Erinnerung ───────────────────────────────────────────────


def test_pending_request_blocks_settlement(db, seed, ev):
    past = seed["past_event"]
    db.add(Booking(event_id=past.id, member_id=seed["member"].id))
    gb = GuestBooking(event_id=past.id, name="Offen", email="o@example.com", count=1)
    db.add(gb)
    db.flush()
    gb.confirmed_at = None
    db.commit()
    assert "Gast-Anfrage" in services.settle_blocker(db, past)


@pytest.mark.anyio
async def test_reminder_skips_pending_guests(db, seed, outbox):
    sub = seed["sub"]
    sub.cancel_hours_free = 36
    event = Event(
        subscription_id=sub.id,
        date=date.today() + timedelta(days=2),
        start_time=datetime.now().time().replace(microsecond=0),
        end_time=seed["event"].end_time,
        max_participants=8,
        min_participants=1,
    )
    db.add(event)
    db.flush()
    db.add(GuestBooking(event_id=event.id, name="Ok", email="ok@example.com", count=1))
    gb = GuestBooking(event_id=event.id, name="Offen", email="offen@example.com", count=1)
    db.add(gb)
    db.flush()
    gb.confirmed_at = None
    db.commit()
    await scheduler.send_cancel_reminders(db)
    recipients = [m["to"] for m in outbox]
    assert "ok@example.com" in recipients
    assert "offen@example.com" not in recipients


def test_schema_upgrade_confirms_existing_guest_bookings(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql("DROP TABLE guest_bookings")
        conn.exec_driver_sql(
            "CREATE TABLE guest_bookings (id VARCHAR(12) PRIMARY KEY, "
            "event_id VARCHAR(12), email VARCHAR(200), name VARCHAR(200), "
            "count INTEGER, token VARCHAR(64), created_at DATETIME)"
        )
        conn.exec_driver_sql(
            "INSERT INTO guest_bookings VALUES ('g1', 'e1', 'a@b.de', 'A', 1, "
            "'t1', '2026-09-01 10:00:00')"
        )
    upgrade(eng)
    upgrade(eng)
    with eng.begin() as conn:
        (confirmed,) = conn.exec_driver_sql(
            "SELECT confirmed_at FROM guest_bookings"
        ).fetchone()
    assert confirmed is not None
