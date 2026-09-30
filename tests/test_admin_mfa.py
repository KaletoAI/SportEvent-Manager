"""Admin-MFA per TOTP: Login mit Passwort + Code, Einrichtung per QR-Code,
in Produktion Pflicht, Reset-Skript für den Notfall."""

import time

import pyotp
import pytest
from sqlalchemy import create_engine

from app import mfa
from app.config import settings
from app.database import SessionLocal
from app.models.models import AppSetting, Base, UserSession
from app.schema_upgrade import upgrade
from tests.conftest import ADMIN_PW, get_csrf


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")


def _login(client, code="", password=ADMIN_PW):
    csrf = get_csrf(client, "/admin/login")
    return client.post(
        "/admin/login",
        data={"password": password, "code": code, "csrf_token": csrf},
        follow_redirects=False,
    )


def _enroll(client) -> str:
    """Einrichtung über die Oberfläche durchspielen; liefert das Geheimnis."""
    resp = _login(client)
    assert resp.status_code == 302
    page = client.get("/admin/mfa/setup")
    assert page.status_code == 200
    db = SessionLocal()
    try:
        (row,) = db.query(AppSetting).filter(
            AppSetting.key.like(mfa.PENDING_KEY + ":%")
        ).all()
        secret = row.value
    finally:
        db.close()
    assert secret and secret in page.text.replace(" ", "")
    csrf = client.cookies.get("csrf_token")
    resp = client.post(
        "/admin/mfa/setup",
        data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/admin/dashboard")
    return secret


def _admin_sessions(db):
    db.expire_all()
    return db.query(UserSession).filter(UserSession.is_admin.is_(True)).all()


# ── Ohne Einrichtung ──────────────────────────────────────────────────────


def test_dev_without_enrollment_logs_in_with_password(client):
    resp = _login(client)
    assert resp.headers["location"] == "/admin/dashboard"
    assert client.get("/admin/dashboard").status_code == 200


def test_production_forces_setup(client, production):
    resp = _login(client)
    assert resp.headers["location"] == "/admin/mfa/setup"
    # alle anderen Admin-Seiten sind gesperrt
    resp = client.get("/admin/dashboard", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/mfa/setup"
    assert client.get("/admin/mfa/setup").status_code == 200


def test_setup_requires_password_login(client):
    resp = client.get("/admin/mfa/setup", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/login"
    resp = client.get("/admin/mfa/qr.svg", follow_redirects=False)
    assert resp.status_code == 302


def test_qr_code_is_svg_and_not_cached(client):
    _login(client)
    client.get("/admin/mfa/setup")
    resp = client.get("/admin/mfa/qr.svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert "no-store" in resp.headers["cache-control"]
    assert b"<svg" in resp.content


def test_setup_with_wrong_code_fails(client):
    _login(client)
    client.get("/admin/mfa/setup")
    csrf = client.cookies.get("csrf_token")
    resp = client.post("/admin/mfa/setup", data={"code": "000000", "csrf_token": csrf},
                       follow_redirects=False)
    assert resp.headers["location"].startswith("/admin/mfa/setup")
    db = SessionLocal()
    try:
        assert not mfa.is_enrolled(db)
    finally:
        db.close()


# ── Nach der Einrichtung ──────────────────────────────────────────────────


def test_enrollment_ends_other_admin_sessions(client, db):
    other = type(client)(client.app)
    _login(other)  # zweite, reine Passwort-Session
    _enroll(client)
    sessions = _admin_sessions(db)
    assert len(sessions) == 1 and sessions[0].mfa_verified
    resp = other.get("/admin/dashboard", follow_redirects=False)
    assert resp.status_code == 302
    assert client.get("/admin/dashboard").status_code == 200


def test_login_needs_password_and_code(client):
    secret = _enroll(client)
    fresh = type(client)(client.app)
    totp = pyotp.TOTP(secret)
    # ohne Code, falscher Code, falsches Passwort mit richtigem Code
    for code, pw in (("", ADMIN_PW), ("123456", ADMIN_PW), (totp.now(), "falsch")):
        resp = _login(fresh, code=code, password=pw)
        assert resp.status_code == 401
        assert "Passwort oder Code falsch" in resp.text
    assert fresh.get("/admin/dashboard", follow_redirects=False).status_code == 302


def test_login_with_valid_code(client, db):
    secret = _enroll(client)
    fresh = type(client)(client.app)
    # Code aus dem nächsten Fenster (Uhr am Handy geht vor). Ältere Fenster
    # als das der Einrichtung sind durch den Replay-Schutz verbraucht.
    resp = _login(fresh, code=pyotp.TOTP(secret).at(time.time() + 30))
    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/dashboard"
    assert fresh.get("/admin/dashboard").status_code == 200


def test_code_cannot_be_reused(client):
    secret = _enroll(client)
    totp = pyotp.TOTP(secret)
    code = totp.at(time.time() + 30)  # frischer Code, nicht der der Einrichtung
    a = type(client)(client.app)
    assert _login(a, code=code).status_code == 302
    b = type(client)(client.app)
    assert _login(b, code=code).status_code == 401


def test_login_page_shows_code_field_once_enrolled(client):
    assert 'name="code"' not in client.get("/admin/login").text
    _enroll(client)
    fresh = type(client)(client.app)
    assert 'name="code"' in fresh.get("/admin/login").text


def test_setup_not_reachable_once_enrolled(client):
    _enroll(client)
    resp = client.get("/admin/mfa/setup", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/dashboard"


def test_reset_disables_mfa_and_logs_out(client, db):
    _enroll(client)
    mfa.reset(db)
    assert not mfa.is_enrolled(db)
    assert _admin_sessions(db) == []
    assert client.get("/admin/dashboard", follow_redirects=False).status_code == 302


def test_previous_window_accepted(db):
    """Uhr am Handy geht nach: Code aus dem vorigen Fenster zählt."""
    secret = pyotp.random_base32()
    mfa._set(db, mfa.SECRET_KEY, secret)
    db.commit()
    assert mfa.verify(db, pyotp.TOTP(secret).at(time.time() - 30))
    assert not mfa.verify(db, pyotp.TOTP(secret).at(time.time() - 90))


def test_schema_upgrade_adds_mfa_flag(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql("DROP TABLE sessions")
        conn.exec_driver_sql(
            "CREATE TABLE sessions (id VARCHAR(12) PRIMARY KEY, token VARCHAR(96), "
            "member_id VARCHAR(12), is_admin BOOLEAN, created_at DATETIME, "
            "expires_at DATETIME)"
        )
        conn.exec_driver_sql(
            "INSERT INTO sessions VALUES ('s1', 't', NULL, 1, "
            "'2026-09-01', '2026-12-01')"
        )
    upgrade(eng)
    upgrade(eng)
    with eng.begin() as conn:
        (flag,) = conn.exec_driver_sql("SELECT mfa_verified FROM sessions").fetchone()
    assert flag == 0
