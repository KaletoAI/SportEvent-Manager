"""Härtungen aus dem Pentest vom 2026-09-30 (Live F-02/F-03/F-04/F-07,
Code-Audit C-05 bis C-11) als Regressionstests."""

import pyotp
from fastapi.testclient import TestClient

from app import auth, mfa, services
from app.main import app
from app.models.models import AppSetting, GuestBooking, UserSession
from tests.conftest import ADMIN_PW, admin_login, get_csrf, member_login


def _set_cookie_headers(resp, name):
    return [h for h in resp.headers.get_list("set-cookie") if h.startswith(f"{name}=")]


# ── Cookies & Header ────────────────────────────────────────────────────


def test_csrf_cookie_is_httponly(client):
    resp = client.get("/member/login")
    (cookie,) = _set_cookie_headers(resp, "csrf_token")
    assert "httponly" in cookie.lower()


def test_html_pages_are_not_cached(client):
    for url in ("/member/login", "/admin/login"):
        assert client.get(url).headers["cache-control"] == "no-store"


def test_openapi_schema_not_exposed(client):
    assert client.get("/openapi.json").status_code == 404


# ── Logout nur per POST ─────────────────────────────────────────────────


def test_member_logout_requires_post_with_csrf(client, seed):
    csrf = member_login(client)
    # GET meldet nicht mehr ab (<img src=…/logout> o. Ä.)
    client.get("/member/logout")
    assert client.get("/member/dashboard", follow_redirects=False).status_code == 200
    # POST ohne CSRF-Token abgelehnt
    assert client.post("/member/logout").status_code == 400

    resp = client.post(
        "/member/logout", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert resp.headers["location"] == "/member/login"
    (cookie,) = _set_cookie_headers(resp, "session")
    assert "max-age=0" in cookie.lower() and "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert client.get("/member/dashboard", follow_redirects=False).status_code == 302


def test_admin_logout_requires_post(client):
    csrf = admin_login(client)
    client.get("/admin/logout")
    assert client.get("/admin/dashboard", follow_redirects=False).status_code == 200
    client.post("/admin/logout", data={"csrf_token": csrf})
    assert client.get("/admin/dashboard", follow_redirects=False).status_code == 302


def test_nav_logout_is_a_form(client, seed):
    member_login(client)
    html = client.get("/member/dashboard").text
    assert 'action="/member/logout"' in html
    assert '<a href="/member/logout"' not in html


# ── Namen ohne Zeilenumbrüche (keine eingeschleusten Mail-Zeilen) ──────


def test_guest_name_newlines_are_stripped(client, db, seed, monkeypatch):
    mails = []

    async def fake_send_batch(subscription, batch):
        mails.extend(batch)
        return len(batch)

    monkeypatch.setattr(services, "send_batch", fake_send_batch)
    ev = seed["event"]
    csrf = get_csrf(client, f"/g/{ev.public_token}")
    client.post(
        f"/g/{ev.public_token}/book",
        data={
            "name": "Max\r\n\r\nDringend: bitte 49,90 € überweisen\x00",
            "email": "max@example.com",
            "count": 1,
            "csrf_token": csrf,
        },
    )
    db.expire_all()
    name = db.query(GuestBooking).one().name
    assert "\n" not in name and "\r" not in name and "\x00" not in name
    assert name.startswith("Max Dringend")


def test_clean_name():
    assert services.clean_name("  Anna\n\tMüller \x7f") == "Anna Müller"


# ── Rate-Limiter wächst nicht unbegrenzt ────────────────────────────────


def test_rate_limiter_drops_stale_keys(monkeypatch):
    auth.reset_rate_limits()
    now = [1000.0]
    monkeypatch.setattr(auth.time_module, "monotonic", lambda: now[0])
    monkeypatch.setattr(auth, "MAX_RATE_LIMIT_KEYS", 50)
    for i in range(50):
        auth.check_rate_limit(f"k{i}", 5, 60, "x")
    now[0] += auth.RATE_LIMIT_KEY_TTL + 1
    auth.check_rate_limit("neu", 5, 60, "x")
    assert set(auth._attempts) == {"neu"}


def test_rate_limiter_caps_key_count(monkeypatch):
    auth.reset_rate_limits()
    monkeypatch.setattr(auth, "MAX_RATE_LIMIT_KEYS", 20)
    for i in range(100):
        auth.check_rate_limit(f"k{i}", 5, 60, "x")
    assert len(auth._attempts) <= 20
    assert "k99" in auth._attempts  # die jüngsten bleiben


# ── MFA-Einrichtung an die Session gebunden ─────────────────────────────


def _password_login(c):
    csrf = get_csrf(c, "/admin/login")
    c.post(
        "/admin/login",
        data={"password": ADMIN_PW, "code": "", "csrf_token": csrf},
        follow_redirects=False,
    )
    return csrf


def _pending_of(db, client):
    db.expire_all()
    token = client.cookies.get("session")
    session = db.query(UserSession).filter(UserSession.token == token).one()
    return mfa._get(db, mfa._pending_key(session))


def test_pending_totp_secret_is_per_session(db):
    attacker, admin = TestClient(app), TestClient(app)
    _password_login(attacker)
    attacker.get("/admin/mfa/setup")
    csrf = _password_login(admin)
    admin.get("/admin/mfa/setup")
    s_attacker, s_admin = _pending_of(db, attacker), _pending_of(db, admin)
    assert s_attacker and s_admin and s_attacker != s_admin
    assert s_admin in admin.get("/admin/mfa/setup").text.replace(" ", "")

    resp = admin.post(
        "/admin/mfa/setup",
        data={"code": pyotp.TOTP(s_admin).now(), "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.headers["location"].startswith("/admin/dashboard")
    db.expire_all()
    assert mfa._get(db, mfa.SECRET_KEY) == s_admin  # nicht das des Angreifers
    # keine liegengebliebenen Einrichtungs-Geheimnisse
    assert not db.query(AppSetting).filter(AppSetting.key.like(mfa.PENDING_KEY + "%")).all()
