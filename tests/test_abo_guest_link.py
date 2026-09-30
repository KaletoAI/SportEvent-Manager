"""Abo-weiter Gast-Link: eine feste URL, die immer den nächsten Termin
buchbar macht — frühestens `guest_days_ahead` Tage vorher."""

from datetime import date, time, timedelta
from decimal import Decimal

from sqlalchemy import create_engine

from app import clock
from app.models.models import Event
from app.schema_upgrade import upgrade
from tests.conftest import admin_login, get_csrf, member_login


def _abo_url(sub):
    return f"/g/abo/{sub.guest_token}"


def _event(db, sub, days, **kw):
    ev = Event(
        subscription_id=sub.id,
        date=date.today() + timedelta(days=days),
        start_time=time(19, 0),
        end_time=time(21, 0),
        max_participants=4,
        min_participants=1,
        abo_budget=Decimal("8.00"),
        normal_budget=Decimal("10.00"),
        **kw,
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


def test_subscription_gets_token_and_default_window(seed):
    sub = seed["sub"]
    assert sub.guest_token and len(sub.guest_token) >= 20
    assert sub.guest_days_ahead == 5
    assert sub.guest_token != seed["other_sub"].guest_token


def test_unknown_token_is_404(client, seed):
    assert client.get("/g/abo/gibtsnicht").status_code == 404


def test_next_event_inside_window_is_bookable(client, seed):
    # seed: vergangener Termin (-3) und nächster Termin in 3 Tagen
    event = seed["event"]
    resp = client.get(_abo_url(seed["sub"]))
    assert resp.status_code == 200
    assert f'action="/g/{event.public_token}/book"' in resp.text


def test_next_event_outside_window_shows_bookable_from(client, db, seed):
    sub = seed["sub"]
    db.delete(seed["event"])
    ev = _event(db, sub, 9)
    resp = client.get(_abo_url(sub))
    assert resp.status_code == 200
    assert ev.public_token not in resp.text
    assert "/book" not in resp.text
    bookable_from = ev.date - timedelta(days=5)
    assert f"{bookable_from.day:02d}.{bookable_from.month:02d}." in resp.text
    assert "buchbar ab" in resp.text


def test_window_is_configurable(client, db, seed):
    sub = seed["sub"]
    db.delete(seed["event"])
    ev = _event(db, sub, 9)
    sub.guest_days_ahead = 10
    db.commit()
    resp = client.get(_abo_url(sub))
    assert f'action="/g/{ev.public_token}/book"' in resp.text


def test_cancelled_and_settled_events_are_skipped(client, db, seed):
    sub = seed["sub"]
    seed["event"].is_cancelled = True
    db.commit()
    settled = _event(db, sub, 1)
    from app.models.models import utcnow

    settled.settled_at = utcnow()
    db.commit()
    nxt = _event(db, sub, 4)
    resp = client.get(_abo_url(sub))
    assert f'action="/g/{nxt.public_token}/book"' in resp.text
    assert seed["event"].public_token not in resp.text
    assert settled.public_token not in resp.text


def test_no_more_events(client, db, seed):
    db.delete(seed["event"])
    db.commit()
    resp = client.get(_abo_url(seed["sub"]))
    assert resp.status_code == 200
    assert "Keine weiteren Termine" in resp.text
    assert "/book" not in resp.text


def test_uses_system_date_override(client, db, seed):
    """Test-Datum 2 Tage vor dem vergangenen Termin → der ist dann der nächste."""
    past = seed["past_event"]
    clock.set_override(db, past.date - timedelta(days=2))
    resp = client.get(_abo_url(seed["sub"]))
    assert f'action="/g/{past.public_token}/book"' in resp.text


def test_booking_via_abo_page_works(client, db, seed):
    event = seed["event"]
    csrf = get_csrf(client, _abo_url(seed["sub"]))
    resp = client.post(
        f"/g/{event.public_token}/book",
        data={"name": "Gast", "email": "gast@example.com", "count": 1,
              "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert f"/g/{event.public_token}/buchung/" in resp.headers["location"]


def test_admin_sees_link_and_can_renew(client, db, seed):
    sub = seed["sub"]
    old = sub.guest_token
    csrf = admin_login(client)
    page = client.get(f"/admin/subscription/{sub.id}")
    assert f"/g/abo/{old}" in page.text
    resp = client.post(
        f"/admin/subscription/{sub.id}/guest-link/renew",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    db.refresh(sub)
    assert sub.guest_token != old
    assert client.get(f"/g/abo/{old}").status_code == 404
    assert client.get(f"/g/abo/{sub.guest_token}").status_code == 200


def test_renew_requires_admin(client, seed):
    csrf = member_login(client)
    resp = client.post(
        f"/admin/subscription/{seed['sub'].id}/guest-link/renew",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303, 401, 403)
    assert seed["sub"].guest_token  # unverändert (kein Crash)


def test_admin_form_saves_days_ahead(client, db, seed):
    sub = seed["sub"]
    csrf = admin_login(client)
    form = client.get(f"/admin/subscription/{sub.id}/edit")
    assert 'name="guest_days_ahead"' in form.text
    resp = client.post(
        f"/admin/subscription/{sub.id}/edit",
        data={
            "csrf_token": csrf,
            "name": sub.name,
            "weekday": sub.weekday,
            "start_time": "18:00",
            "duration_minutes": 120,
            "start_date": sub.start_date.isoformat(),
            "end_date": sub.end_date.isoformat(),
            "default_price": "20",
            "abo_price": "16",
            "max_participants": 4,
            "min_participants": 1,
            "cancel_hours_free": 48,
            "cancel_hours_approval": 0,
            "guest_days_ahead": 7,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    db.refresh(sub)
    assert sub.guest_days_ahead == 7


def test_super_sees_abo_link_regular_member_not(client, seed):
    sub = seed["sub"]
    member_login(client, "sina@example.com")
    assert f"/g/abo/{sub.guest_token}" in client.get("/member/dashboard").text

    other = type(client)(client.app)
    member_login(other, "anna@example.com")
    assert f"/g/abo/{sub.guest_token}" not in other.get("/member/dashboard").text


def test_schema_upgrade_backfills_tokens(tmp_path):
    from app.models.models import Base

    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        # subscriptions im Stand vor dem Abo-Gastlink nachbauen
        conn.exec_driver_sql("DROP TABLE subscriptions")
        conn.exec_driver_sql(
            "CREATE TABLE subscriptions (id VARCHAR(12) PRIMARY KEY, "
            "name VARCHAR(200), abo_price NUMERIC, default_price NUMERIC, "
            "min_participants INTEGER, cancel_hours_free INTEGER, "
            "cancel_hours_approval INTEGER, payout_mode VARCHAR(10))"
        )
        for i in range(2):
            conn.exec_driver_sql(
                "INSERT INTO subscriptions (id, name) VALUES (?, 'x')", (f"s{i}",)
            )
    upgrade(eng)
    upgrade(eng)  # idempotent
    with eng.begin() as conn:
        rows = conn.exec_driver_sql(
            "SELECT guest_token, guest_days_ahead FROM subscriptions"
        ).fetchall()
    tokens = [t for t, _ in rows]
    assert all(tokens) and len(set(tokens)) == 2
    assert all(d == 5 for _, d in rows)
