"""Direktlinks in Mails (nur mit BASE_URL) und Login mit Zielseite."""

from urllib.parse import parse_qs, urlparse

import pytest

from app import scheduler, services
from app.config import settings
from app.emailer import cancel_reminder_email
from app.models.models import Booking, GuestBooking, Member
from app.routes import member as member_routes
from app.web import safe_next
from tests.conftest import get_csrf, make_login_token, member_login


@pytest.fixture
def outbox(monkeypatch):
    mails = []

    async def fake_send_batch(subscription, batch):
        mails.extend({"to": m.to, "subject": m.subject, "body": m.body} for m in batch)
        return len(batch)

    for mod in (services, scheduler, member_routes):
        monkeypatch.setattr(mod, "send_batch", fake_send_batch)
    return mails


@pytest.fixture
def base_url(monkeypatch):
    monkeypatch.setattr(settings, "base_url", "https://abo.example/")
    return "https://abo.example/"


def test_no_button_without_base_url():
    body = cancel_reminder_email("Anna", "01.01.2030", "31.12.2029 18:00").text
    assert "http" not in body


def test_cancel_request_mail_links_to_admin_tab(client, db, seed, outbox, base_url):
    event = seed["event"]
    sub = seed["sub"]
    sub.cancel_hours_free = 24 * 7  # Termin in 3 Tagen → Freigabe nötig
    sub.cancel_hours_approval = 1
    db.add(Booking(event_id=event.id, member_id=seed["member"].id))
    db.commit()
    csrf = member_login(client)
    client.post(f"/member/event/{event.id}/unbook", data={"csrf_token": csrf})
    assert [m["to"] for m in outbox] == ["sina@example.com"]
    assert f"{base_url}member/dashboard?tab=verwaltung&abo={sub.id}" in outbox[0]["body"]


def test_settlement_and_guest_mails_carry_links(client, db, seed, outbox, base_url):
    event = seed["past_event"]
    db.add(Booking(event_id=event.id, member_id=seed["member"].id))
    gb = GuestBooking(event_id=event.id, name="Gerd", email="gerd@example.com", count=1)
    db.add(gb)
    db.commit()
    import anyio

    anyio.run(services.send_guest_confirmation, db, gb)
    anyio.run(services.settle_and_notify, db, event)
    by_to = {}
    for m in outbox:
        by_to.setdefault(m["to"], []).append(m["body"])
    assert f"{base_url}g/{event.public_token}/buchung/{gb.token}" in by_to["gerd@example.com"][0]
    assert f"tab=konto&abo={seed['sub'].id}" in by_to["anna@example.com"][0]


def test_minimum_mail_contains_guest_link(client, db, seed, outbox, base_url):
    event = seed["event"]
    event.min_participants = 2
    db.add_all(
        [
            Booking(event_id=event.id, member_id=seed["member"].id),
            Booking(event_id=event.id, member_id=seed["super_member"].id),
        ]
    )
    db.commit()
    csrf = member_login(client)
    client.post(f"/member/event/{event.id}/unbook", data={"csrf_token": csrf})
    search = [m for m in outbox if m["subject"].startswith("Mitspieler gesucht")]
    assert search and f"{base_url}g/{event.public_token}" in search[0]["body"]


def test_login_redirect_keeps_target_through_magic_link(client, db, seed):
    target = f"/member/dashboard?tab=verwaltung&abo={seed['sub'].id}"
    resp = client.get(target, follow_redirects=False)
    login_url = resp.headers["location"]
    assert parse_qs(urlparse(login_url).query)["next"] == [target]
    page = client.get(login_url).text
    assert 'name="next"' in page

    token = make_login_token("sina@example.com")
    confirm = client.get(f"/member/login/t/{token}", params={"next": target}).text
    assert 'name="next"' in confirm
    resp = client.post(
        f"/member/login/t/{token}",
        data={"csrf_token": client.cookies.get("csrf_token"), "next": target},
        follow_redirects=False,
    )
    assert resp.headers["location"] == target


def test_login_request_puts_target_into_link(client, db, seed):
    """Dev ohne SMTP zeigt die Links an — dort muss das Ziel mitwandern."""
    target = "/member/event/abc/participants"
    csrf = get_csrf(client, "/member/login")
    page = client.post(
        "/member/login",
        data={"email": "anna@example.com", "csrf_token": csrf, "next": target},
    ).text
    assert "?next=%2Fmember%2Fevent%2Fabc%2Fparticipants" in page


@pytest.mark.parametrize(
    "value",
    ["https://evil.example/", "//evil.example", "/member/\\evil", "/admin/dashboard",
     "/member/login?x=1", "", "/member/\nx"],
)
def test_safe_next_rejects_foreign_targets(value):
    assert safe_next(value) == "/member/dashboard"


def test_switch_offer_for_other_abo(client, db, seed):
    anna2 = Member(
        subscription_id=seed["other_sub"].id, email="anna@example.com", name="Anna"
    )
    db.add(anna2)
    db.commit()
    member_login(client)
    html = client.get(f"/member/dashboard?tab=konto&abo={seed['other_sub'].id}").text
    assert f'action="/member/switch/{anna2.id}"' in html
    assert 'name="next" value="/member/dashboard?tab=konto"' in html
    # Eigenes Abo → kein Hinweis
    html = client.get(f"/member/dashboard?abo={seed['sub'].id}").text
    assert "Der Link gehört zum Abo" not in html
