"""Regressionstests aus dem Security-Review (XSS, Login-Härtung, Flash)."""

import re

import pytest
from conftest import admin_login, get_csrf, make_login_token, member_login

from app import emailer
from app.models.models import GuestBooking, LoginToken, Member, WaitlistEntry
from app.web import flash_signature


def _dev_login_request(client, email):
    csrf = get_csrf(client, "/member/login")
    return client.post("/member/login", data={"email": email, "csrf_token": csrf})


# ── XSS / CSP ──────────────────────────────────────────────────────────────


def test_guest_name_cannot_break_out_of_confirm(client, db, seed):
    """Gastname mit ' landete früher in onsubmit="confirm('…')" → XSS."""
    event = seed["event"]
    csrf = get_csrf(client, f"/g/{event.public_token}")
    payload = "x');alert(document.domain);('"
    client.post(
        f"/g/{event.public_token}/book",
        data={"name": payload, "email": "a@b.de", "count": 1, "csrf_token": csrf},
    )
    admin_login(client)
    html = client.get(f"/admin/event/{event.id}").text
    assert "onsubmit" not in html and "onclick" not in html
    # Nur noch als Attributwert, der nie als Code ausgewertet wird
    assert 'data-confirm="Gastbuchung von x&#39;);alert(document.domain);(&#39; entfernen?"' in html


@pytest.mark.parametrize("url", ["/member/login", "/admin/login", "/hilfe"])
def test_csp_forbids_inline_scripts(client, url):
    csp = client.get(url).headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp


def test_templates_have_no_inline_scripts():
    from pathlib import Path

    for path in Path("app/templates").rglob("*.html"):
        text = path.read_text()
        assert not re.search(r"\son[a-z]+=", text), path
        assert "<script>" not in text, path
        assert 'style="' not in text, path


def test_help_documents_get_their_own_csp(client):
    resp = client.get("/hilfe/kurzreferenz")
    if resp.status_code == 200:
        assert "default-src 'none'" in resp.headers["content-security-policy"]


def test_login_mail_html_escapes_name():
    html = emailer.login_link_email_html("<b>Eve</b>", "https://x/?a=1&b=2", "123456")
    assert "<b>Eve</b>" not in html
    assert "&lt;b&gt;Eve&lt;/b&gt;" in html


# ── Login: keine Enumeration, Code-Sperre, nur neuester Link ───────────────


def test_login_answer_is_identical_for_known_and_unknown(client, seed, monkeypatch):
    """Mit SMTP verriet die Meldung früher, ob die Adresse existiert."""
    import app.routes.member as member_routes

    sent = []

    async def fake_send(config, mails):
        sent.extend(mails)
        return len(mails)

    monkeypatch.setattr(member_routes, "smtp_config_for", lambda sub: {"host": "x"})
    monkeypatch.setattr(member_routes, "send_with_config", fake_send)

    def flash(text):
        return re.search(r'<div class="msg msg-success">(.*?)</div>', text).group(1)

    known = _dev_login_request(client, "anna@example.com").text
    unknown = _dev_login_request(client, "nobody@example.com").text
    assert flash(known) == flash(unknown)
    assert "/member/login/t/" not in known  # mit SMTP nie Links auf der Seite
    assert [m.to for m in sent] == ["anna@example.com"]  # Versand im Hintergrund


def test_code_is_burnt_after_too_many_wrong_attempts(client, db, seed):
    resp = _dev_login_request(client, "anna@example.com")
    code = re.search(r"Code: <strong>(\d{6})</strong>", resp.text).group(1)
    wrong = "000000" if code != "000000" else "111111"
    csrf = client.cookies.get("csrf_token")
    for _ in range(5):
        r = client.post(
            "/member/login/code",
            data={"email": "anna@example.com", "code": wrong, "csrf_token": csrf},
        )
        assert r.status_code == 401
    db.expire_all()
    assert db.query(LoginToken).count() == 0
    r = client.post(
        "/member/login/code",
        data={"email": "anna@example.com", "code": code, "csrf_token": csrf},
    )
    assert r.status_code == 401  # auch der richtige Code gilt nicht mehr


def test_only_newest_login_request_is_valid(client, seed):
    first = _dev_login_request(client, "anna@example.com").text
    old = re.search(r"/member/login/t/([A-Za-z0-9_-]+)", first).group(1)
    _dev_login_request(client, "anna@example.com")
    resp = client.get(f"/member/login/t/{old}", follow_redirects=False)
    assert "ung%C3%BCltig" in resp.headers["location"]


def test_login_requests_are_limited_per_address(client, seed):
    for _ in range(5):
        assert _dev_login_request(client, "anna@example.com").status_code == 200
    assert _dev_login_request(client, "Anna@Example.com ").status_code == 429


def test_link_preview_does_not_consume_token(client, seed):
    token = make_login_token("anna@example.com")
    for _ in range(3):  # z. B. Mail-Scanner + Vorschau
        assert client.get(f"/member/login/t/{token}").status_code == 200
    csrf = client.cookies.get("csrf_token")
    resp = client.post(
        f"/member/login/t/{token}", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert resp.headers["location"] == "/member/dashboard"


def test_email_login_is_case_insensitive(client, db, seed):
    resp = _dev_login_request(client, "  ANNA@Example.COM ")
    assert "/member/login/t/" in resp.text


def test_admin_stores_emails_normalized(client, db, seed):
    csrf = admin_login(client)
    sub = seed["sub"]
    client.post(
        f"/admin/subscription/{sub.id}/members/new",
        data={"name": "Kai", "email": " Kai@Example.DE ", "csrf_token": csrf},
    )
    assert db.query(Member).filter(Member.email == "kai@example.de").count() == 1


# ── Flash-Meldungen nur aus eigenen Redirects ──────────────────────────────


def test_unsigned_flash_message_is_not_shown(client):
    text = client.get("/member/login?msg=Bitte+PayPal+an+evil%40x.de&mt=success").text
    assert "evil@x.de" not in text


def test_signed_flash_message_is_shown(client):
    msg = "Alles gut"
    sig = flash_signature(msg, "success")
    text = client.get(f"/member/login?msg=Alles%20gut&mt=success&sig={sig}").text
    assert "Alles gut" in text


# ── Warteliste: verwaiste Einträge ─────────────────────────────────────────


def test_deleting_waitlisted_member_does_not_break_promotion(client, db, seed):
    """Früher: 500 (AttributeError) bei der nächsten Platzfreigabe."""
    event = seed["event"]
    m = Member(subscription_id=event.subscription_id, email="w@x.de", name="W")
    db.add(m)
    db.flush()
    db.add(WaitlistEntry(event_id=event.id, member_id=m.id))
    db.commit()
    csrf = admin_login(client)
    client.post(f"/admin/member/{m.id}/delete", data={"csrf_token": csrf})
    db.expire_all()
    assert db.query(WaitlistEntry).count() == 0

    client.post(
        f"/admin/event/{event.id}/book-guest",
        data={"name": "G", "count": 1, "csrf_token": csrf},
    )
    gb = db.query(GuestBooking).one()
    resp = client.post(
        f"/admin/guest-booking/{gb.id}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302


@pytest.mark.anyio
async def test_promotion_skips_inactive_and_stale_entries(db, seed):
    from app import services
    from app.models.models import Booking

    event = seed["event"]
    member = seed["member"]
    inactive = Member(
        subscription_id=event.subscription_id, email="i@x.de", name="I", is_active=False
    )
    db.add(inactive)
    db.flush()
    db.add(WaitlistEntry(event_id=event.id, member_id=inactive.id))
    # Anna ist schon direkt gebucht, steht aber noch auf der Liste
    db.add(Booking(event_id=event.id, member_id=member.id))
    db.add(WaitlistEntry(event_id=event.id, member_id=member.id))
    db.commit()
    promoted = await services.promote_from_waitlist(db, event)
    assert promoted == []
    assert db.query(WaitlistEntry).count() == 0


def test_direct_booking_removes_own_waitlist_entry(client, db, seed):
    event = seed["event"]
    db.add(WaitlistEntry(event_id=event.id, member_id=seed["member"].id))
    db.commit()
    csrf = member_login(client)
    client.post(
        f"/member/event/{event.id}/book", data={"guest_count": 0, "csrf_token": csrf}
    )
    db.expire_all()
    assert db.query(WaitlistEntry).count() == 0


# ── Housekeeping ───────────────────────────────────────────────────────────


def test_purge_expired_sessions_and_tokens(db, seed):
    from datetime import timedelta

    from app.auth import purge_expired
    from app.models.models import UserSession, utcnow

    db.add(UserSession(token="old", expires_at=utcnow() - timedelta(days=1)))
    db.add(UserSession(token="new", expires_at=utcnow() + timedelta(days=1)))
    db.add(
        LoginToken(
            token="t",
            member_id=seed["member"].id,
            expires_at=utcnow() - timedelta(minutes=1),
        )
    )
    db.commit()
    assert purge_expired(db) == 2
    assert [s.token for s in db.query(UserSession).all()] == ["new"]


# ── Mailversand: eine SMTP-Verbindung pro Stapel ───────────────────────────


@pytest.mark.anyio
async def test_batch_uses_one_smtp_connection(monkeypatch):
    connections = []

    class FakeSMTP:
        def __init__(self, **kwargs):
            self.sent = []
            connections.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def login(self, user, password):
            pass

        async def send_message(self, message):
            if message["To"] == "bad@x.de":
                raise RuntimeError("rejected")
            self.sent.append(message["To"])

    monkeypatch.setattr(emailer.aiosmtplib, "SMTP", FakeSMTP)
    config = {
        "host": "smtp", "port": 465, "user": "u", "password": "p",
        "use_tls": True, "sender": "a@b.de", "sender_name": "SportAbo",
    }
    mails = [emailer.Mail(to, "S", "B") for to in ("a@x.de", "bad@x.de", "c@x.de")]
    assert await emailer.send_with_config(config, mails) == 2
    assert len(connections) == 1
    assert connections[0].sent == ["a@x.de", "c@x.de"]
