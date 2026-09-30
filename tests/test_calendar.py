"""Kalender-Abo: persönlicher iCal-Feed über alle Abos einer Person."""

import re
from datetime import date, time, timedelta
from decimal import Decimal

from conftest import get_csrf, member_login

from app import ical
from app.models.models import Booking, Event, Member, Person


def _event(db, sub, day, **kw):
    e = Event(
        subscription_id=sub.id,
        date=day,
        start_time=time(18, 30),
        end_time=time(20, 30),
        max_participants=8,
        min_participants=1,
        abo_budget=Decimal("8.00"),
        normal_budget=Decimal("10.00"),
        **kw,
    )
    db.add(e)
    db.flush()
    return e


def _feed_url(html):
    """Pfad des Kalender-Links (Host hängt von BASE_URL ab)."""
    return re.search(r'id="calendar-link" value="[^"]*?(/kalender/[^"]+\.ics)"', html).group(1)


# ── ICS-Format ──────────────────────────────────────────────────────────


def test_ical_escape_and_fold():
    assert ical.escape("a,b;c\\d\ne") == "a\\,b\\;c\\\\d\\ne"
    line = "SUMMARY:" + "Ä" * 60  # 128 Bytes UTF-8
    folded = ical.fold(line)
    parts = folded.split("\r\n")
    assert len(parts) > 1
    assert all(len(p.encode()) <= 75 for p in parts)
    assert all(p.startswith(" ") for p in parts[1:])
    assert "".join(p[1:] if i else p for i, p in enumerate(parts)) == line


# ── Feed ────────────────────────────────────────────────────────────────


def test_feed_spans_all_abos(client, db, seed):
    anna = seed["member"]
    anna2 = Member(
        subscription_id=seed["other_sub"].id,
        email="anna@example.com",
        name="Anna",
        password_hash="",
    )
    db.add(anna2)
    db.flush()
    soon = _event(db, seed["other_sub"], date.today() + timedelta(days=5))
    cancelled = _event(
        db, seed["sub"], date.today() + timedelta(days=9), is_cancelled=True
    )
    foreign = _event(db, seed["other_sub"], date.today() + timedelta(days=12))
    db.add_all(
        [
            Booking(member_id=anna.id, event_id=seed["event"].id, guest_count=1),
            Booking(member_id=anna2.id, event_id=soon.id),
            Booking(member_id=anna.id, event_id=cancelled.id),
            Booking(member_id=seed["outsider"].id, event_id=foreign.id),
        ]
    )
    db.commit()

    member_login(client)
    url = _feed_url(client.get("/member/dashboard").text)

    anon = client.__class__(client.app)  # Kalender-Apps haben keine Session
    resp = anon.get(url)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/calendar")
    body = resp.text
    assert body.startswith("BEGIN:VCALENDAR\r\n")
    assert body.count("BEGIN:VEVENT") == 3
    assert f"UID:event-{seed['event'].id}@sportabo" in body
    assert f"UID:event-{soon.id}@sportabo" in body
    assert f"UID:event-{foreign.id}@sportabo" not in body
    assert "SUMMARY:Beachvolleyball (+1 Gast)" in body
    assert "SUMMARY:Fußball" in body
    assert "STATUS:CANCELLED" in body
    d = seed["event"].date.strftime("%Y%m%d")
    assert f"DTSTART;TZID=Europe/Berlin:{d}T180000" in body
    assert "BEGIN:VTIMEZONE" in body


def test_feed_skips_inactive_membership_and_unknown_token(client, db, seed):
    anna2 = Member(
        subscription_id=seed["other_sub"].id,
        email="anna@example.com",
        name="Anna",
        password_hash="",
        is_active=False,
    )
    db.add(anna2)
    db.flush()
    e = _event(db, seed["other_sub"], date.today() + timedelta(days=4))
    db.add(Booking(member_id=anna2.id, event_id=e.id))
    db.commit()

    member_login(client)
    url = _feed_url(client.get("/member/dashboard").text)
    assert "BEGIN:VEVENT" not in client.get(url).text
    assert client.get("/kalender/gibtsnicht.ics").status_code == 404


def test_renew_calendar_link(client, db, seed):
    csrf = member_login(client)
    old = _feed_url(client.get("/member/dashboard").text)
    assert client.get(old).status_code == 200

    # Ohne CSRF-Token abgelehnt
    resp = client.post("/member/calendar/renew", follow_redirects=False)
    assert resp.status_code == 400

    resp = client.post(
        "/member/calendar/renew", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert resp.status_code == 302
    new = _feed_url(client.get("/member/dashboard").text)
    assert new != old
    assert client.get(old).status_code == 404
    assert client.get(new).status_code == 200


def test_calendar_token_lives_on_person(client, db, seed):
    """Ein Link pro Person — nach dem Abo-Wechsel bleibt er gleich."""
    anna2 = Member(
        subscription_id=seed["other_sub"].id,
        email="anna@example.com",
        name="Anna",
        password_hash="",
    )
    db.add(anna2)
    db.commit()
    csrf = member_login(client)
    first = _feed_url(client.get("/member/dashboard").text)
    client.post(
        f"/member/switch/{anna2.id}", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert _feed_url(client.get("/member/dashboard").text) == first
    db.expire_all()
    person = db.query(Person).filter(Person.email == "anna@example.com").one()
    assert first.endswith(f"/{person.calendar_token}.ics")
