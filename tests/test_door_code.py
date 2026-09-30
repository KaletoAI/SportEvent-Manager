"""Türcode: pro Saison (Abo) oder pro Termin; Mitglieder sehen ihn in der
App, Link-Gäste erst mit der bestätigten Buchung (Mail + Bestätigungsseite)."""

import os
import subprocess
import sys
from datetime import datetime

import pytest

from app import scheduler, services
from app.database import database_url
from app.models.models import Booking, Event, GuestBooking
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


def _book_guest(client, event, email="gast@example.com", count=1):
    csrf = get_csrf(client, f"/g/{event.public_token}")
    return client.post(
        f"/g/{event.public_token}/book",
        data={"name": "Gast", "email": email, "count": count, "csrf_token": csrf},
        follow_redirects=False,
    )


# ── Effektiver Code ─────────────────────────────────────────────────────


def test_event_code_overrides_season_code(db, seed):
    ev, sub = seed["event"], seed["sub"]
    assert services.door_code(ev) == ""
    sub.door_code = "1111"
    assert services.door_code(ev) == "1111"
    ev.door_code = "2222"
    assert services.door_code(ev) == "2222"


# ── Admin pflegt Codes ──────────────────────────────────────────────────


def test_admin_sets_event_code_and_members_see_it(client, db, seed):
    ev = seed["event"]
    csrf = admin_login(client)
    resp = client.post(
        f"/admin/event/{ev.id}/door-code",
        data={"door_code": " 0815 ", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    db.expire_all()
    assert db.get(Event, ev.id).door_code == "0815"
    assert "0815" in client.get(f"/admin/event/{ev.id}").text

    member_login(client)
    html = client.get("/member/dashboard").text
    assert "Türcode" in html and "0815" in html
    assert "0815" in client.get(f"/member/event/{ev.id}/participants").text


def test_season_code_via_subscription_form(client, db, seed):
    sub = seed["sub"]
    csrf = admin_login(client)
    form = {
        "name": sub.name,
        "weekday": sub.weekday,
        "start_time": "18:00",
        "start_date": sub.start_date.isoformat(),
        "end_date": sub.end_date.isoformat(),
        "door_code": "4711",
        "csrf_token": csrf,
    }
    client.post(f"/admin/subscription/{sub.id}/edit", data=form, follow_redirects=False)
    db.expire_all()
    assert db.get(type(sub), sub.id).door_code == "4711"
    member_login(client)
    assert "4711" in client.get("/member/dashboard").text


def test_other_abo_members_dont_see_code(client, db, seed):
    seed["event"].door_code = "9876"
    db.commit()
    member_login(client, email="bernd@example.com")
    assert "9876" not in client.get("/member/dashboard").text


# ── Gäste ───────────────────────────────────────────────────────────────


def test_confirmed_guest_gets_code_by_mail_and_page(client, db, seed, outbox):
    ev = seed["event"]  # min 1 → erste Gastbuchung sofort bestätigt
    ev.door_code = "5555"
    db.commit()
    resp = _book_guest(client, ev)
    assert [m["to"] for m in outbox] == ["gast@example.com"]
    assert "bestätigt" in outbox[0]["subject"]
    assert "5555" in outbox[0]["body"]
    assert "5555" in client.get(resp.headers["location"]).text
    # öffentliche Buchungsseite verrät den Code nicht
    assert "5555" not in client.get(f"/g/{ev.public_token}").text


def test_guest_request_sees_code_only_after_confirmation(client, db, seed, outbox):
    ev = seed["event"]
    ev.door_code = "6666"
    db.add(Booking(event_id=ev.id, member_id=seed["member"].id))  # Mindestzahl 1 erreicht
    db.commit()
    resp = _book_guest(client, ev)
    assert all("6666" not in m["body"] for m in outbox)
    assert "6666" not in client.get(resp.headers["location"]).text

    outbox.clear()
    db.expire_all()
    gb = db.query(GuestBooking).one()
    import asyncio

    asyncio.run(services.confirm_guest_booking(db, gb))
    assert "6666" in outbox[0]["body"]
    assert "6666" in client.get(resp.headers["location"]).text


# ── Erinnerungsmails ────────────────────────────────────────────────────


def test_reminder_mails_contain_code(client, db, seed, outbox):
    ev = seed["event"]
    ev.door_code = "7777"
    db.add(Booking(event_id=ev.id, member_id=seed["member"].id))
    db.add(GuestBooking(event_id=ev.id, name="G", email="g@example.com", count=1))
    # Letzte freie Abmeldung in 12 h → Erinnerung fällig
    start = datetime.combine(ev.date, ev.start_time)
    seed["sub"].cancel_hours_free = int(
        (start - datetime.now()).total_seconds() // 3600
    ) - 12
    db.commit()
    import asyncio

    asyncio.run(scheduler.send_cancel_reminders(db))
    assert len(outbox) == 2
    assert all("7777" in m["body"] for m in outbox)


# ── Termine-Liste: Teilnehmerzahl hinter „Findet statt“ ────────────────


def test_findet_statt_shows_participant_count(client, db, seed):
    ev = seed["event"]
    db.add(Booking(event_id=ev.id, member_id=seed["member"].id, guest_count=2))
    db.add(GuestBooking(event_id=ev.id, name="G", email="g@example.com", count=1))
    db.commit()
    member_login(client)
    assert "Findet statt (4)" in client.get("/member/dashboard").text
    admin_login(client)
    assert "Findet statt (4)" in client.get(f"/admin/subscription/{seed['sub'].id}").text


# ── Einmalige Übernahme per Skript ──────────────────────────────────────


def test_import_script_sets_codes(db, seed, tmp_path):
    ev = seed["event"]
    lines = f"{ev.date.isoformat()} 012345\n2099-01-01 999999\n"
    result = subprocess.run(
        [sys.executable, "scripts/set_door_codes.py", seed["sub"].name, "--season", "3333"],
        input=lines,
        capture_output=True,
        text=True,
        # dieselbe Test-DB wie die App (os.environ kann abweichen, wenn
        # conftest doppelt importiert wurde)
        env={**os.environ, "DATABASE_URL": database_url},
    )
    assert result.returncode == 0, result.stderr
    assert "1 Termin" in result.stdout
    assert "2099-01-01" in result.stdout  # nicht gefundenes Datum gemeldet
    db.expire_all()
    assert db.get(Event, ev.id).door_code == "012345"
    assert db.get(type(seed["sub"]), seed["sub"].id).door_code == "3333"


def test_calendar_feed_contains_code(client, db, seed):
    ev = seed["event"]
    ev.door_code = "8888"
    db.add(Booking(event_id=ev.id, member_id=seed["member"].id))
    db.commit()
    member_login(client)
    html = client.get("/member/dashboard").text
    path = html.split('id="calendar-link" value="', 1)[1].split('"', 1)[0]
    path = "/kalender/" + path.split("/kalender/", 1)[1]
    assert "Türcode: 8888" in client.get(path).text
