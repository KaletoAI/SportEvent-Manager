"""Auth: sessions, password hashing, rate limiting, CSRF."""

from conftest import ADMIN_PW, admin_login, get_csrf, member_login

from app.auth import hash_password, verify_password, password_needs_rehash
from app.models.models import Member, UserSession


def test_password_hash_roundtrip():
    h = hash_password("hunter22")
    assert h.startswith("$argon2")
    assert verify_password("hunter22", h)
    assert not verify_password("wrong", h)


def test_legacy_hash_verifies_and_flags_rehash():
    # Old format: salt_hex:sha256(salt+pw)
    import hashlib, os

    salt = os.urandom(32)
    legacy = salt.hex() + ":" + hashlib.sha256(salt + b"oldpw").hexdigest()
    assert verify_password("oldpw", legacy)
    assert not verify_password("wrong", legacy)
    assert password_needs_rehash(legacy)
    assert not verify_password("x", "garbage-without-colon")  # no crash


def test_admin_login_and_logout(client, db):
    admin_login(client)
    assert db.query(UserSession).filter(UserSession.is_admin).count() == 1
    resp = client.get("/admin/dashboard")
    assert resp.status_code == 200

    client.get("/admin/logout", follow_redirects=False)
    db.expire_all()
    assert db.query(UserSession).count() == 0
    resp = client.get("/admin/dashboard", follow_redirects=False)
    assert resp.status_code == 302  # back to login


def test_admin_wrong_password(client):
    csrf = get_csrf(client, "/admin/login")
    resp = client.post(
        "/admin/login", data={"password": "nope", "csrf_token": csrf}
    )
    assert resp.status_code == 401


def test_secret_key_is_not_a_valid_session(client):
    """The old scheme accepted the secret key as admin cookie — must not work."""
    client.cookies.set("session", "test-secret-key-not-for-production-abc123")
    resp = client.get("/admin/dashboard", follow_redirects=False)
    assert resp.status_code == 302


def test_member_token_login_flow(client, db, seed):
    """Magic Link: E-Mail anfordern → Dev-Link → Session."""
    csrf = get_csrf(client, "/member/login")
    resp = client.post(
        "/member/login",
        data={"email": "anna@example.com", "csrf_token": csrf},
    )
    assert resp.status_code == 200
    # Dev-Modus ohne SMTP: Link steht auf der Seite
    assert "/member/login/t/" in resp.text
    import re

    token = re.search(r"/member/login/t/([A-Za-z0-9_-]+)", resp.text).group(1)
    resp = client.get(f"/member/login/t/{token}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/member/dashboard"
    # Token ist einmalig
    client.get("/member/logout")
    resp = client.get(f"/member/login/t/{token}", follow_redirects=False)
    assert "ung%C3%BCltig" in resp.headers["location"]


def test_unknown_email_gets_neutral_answer(client, seed):
    csrf = get_csrf(client, "/member/login")
    resp = client.post(
        "/member/login",
        data={"email": "gibtsnicht@example.com", "csrf_token": csrf},
    )
    assert resp.status_code == 200
    assert "/member/login/t/" not in resp.text  # kein Link geleakt


def test_expired_token_rejected(client, db, seed):
    from datetime import timedelta

    from conftest import make_login_token
    from app.models.models import LoginToken, utcnow

    token = make_login_token("anna@example.com")
    row = db.query(LoginToken).filter(LoginToken.token == token).one()
    row.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    resp = client.get(f"/member/login/t/{token}", follow_redirects=False)
    assert "abgelaufen" in resp.headers["location"]
    db.expire_all()
    assert db.query(LoginToken).count() == 0  # aufgeräumt


def test_member_login_deactivated(client, db, seed):
    member = seed["member"]
    member.is_active = False
    db.commit()
    # Kein Link für deaktivierte Konten
    csrf = get_csrf(client, "/member/login")
    resp = client.post(
        "/member/login",
        data={"email": member.email, "csrf_token": csrf},
    )
    assert "/member/login/t/" not in resp.text


def test_login_rate_limit(client):
    csrf = get_csrf(client, "/admin/login")
    for _ in range(10):
        resp = client.post(
            "/admin/login", data={"password": "nope", "csrf_token": csrf}
        )
        assert resp.status_code == 401
    resp = client.post(
        "/admin/login", data={"password": ADMIN_PW, "csrf_token": csrf}
    )
    assert resp.status_code == 429


def test_csrf_required_on_post(client, seed):
    member_login(client)
    event = seed["event"]
    # Missing token
    resp = client.post(f"/member/event/{event.id}/book", data={"guest_count": "0"})
    assert resp.status_code == 400
    # Wrong token
    resp = client.post(
        f"/member/event/{event.id}/book",
        data={"guest_count": "0", "csrf_token": "forged"},
    )
    assert resp.status_code == 400


def test_code_login_flow(client, db, seed):
    """6-stelliger Code als Alternative zum Link."""
    import re

    csrf = get_csrf(client, "/member/login")
    resp = client.post(
        "/member/login",
        data={"email": "anna@example.com", "csrf_token": csrf},
    )
    assert "Code eingeben" in resp.text
    code = re.search(r"Code: <strong>(\d{6})</strong>", resp.text).group(1)

    resp = client.post(
        "/member/login/code",
        data={"email": "anna@example.com", "code": code, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/member/dashboard"

    # Code ist einmalig
    client.get("/member/logout")
    resp = client.post(
        "/member/login/code",
        data={"email": "anna@example.com", "code": code, "csrf_token": csrf},
    )
    assert resp.status_code == 401


def test_wrong_code_rejected(client, db, seed):
    csrf = get_csrf(client, "/member/login")
    client.post(
        "/member/login",
        data={"email": "anna@example.com", "csrf_token": csrf},
    )
    resp = client.post(
        "/member/login/code",
        data={"email": "anna@example.com", "code": "000000", "csrf_token": csrf},
    )
    # (1:1'000'000-Chance auf Kollision akzeptiert)
    assert resp.status_code == 401
    assert "ungültig" in resp.text


def test_base_url_used_in_login_links(client, db, seed, monkeypatch):
    from app.config import settings as s

    monkeypatch.setattr(s, "base_url", "https://sportabo.example.com")
    csrf = get_csrf(client, "/member/login")
    resp = client.post(
        "/member/login",
        data={"email": "anna@example.com", "csrf_token": csrf},
    )
    assert "https://sportabo.example.com/member/login/t/" in resp.text


def test_login_page_redirects_when_already_logged_in(client, db, seed):
    """PWA-Neustart: Login-Seiten schicken eingeloggte Nutzer ins Dashboard."""
    member_login(client)
    resp = client.get("/member/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/member/dashboard"

    client.get("/member/logout")
    admin_login(client)
    resp = client.get("/admin/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/dashboard"


def test_session_sliding_expiration(client, db, seed):
    """Aktive Nutzung verlängert die Session (Gerät bleibt angemeldet)."""
    from datetime import timedelta

    from app.models.models import UserSession, utcnow

    member_login(client)
    session = db.query(UserSession).one()
    # Session steht kurz vor dem Ablauf …
    session.expires_at = utcnow() + timedelta(days=2)
    db.commit()
    # … ein Aufruf genügt, um sie wieder auf volle Laufzeit zu setzen
    assert client.get("/member/dashboard").status_code == 200
    db.expire_all()
    session = db.query(UserSession).one()
    assert session.expires_at > utcnow() + timedelta(days=28)


def test_membership_switcher(client, db, seed):
    """Gleiche E-Mail in zwei Abos: Umschalter wechselt die Session."""
    from app.models.models import Member

    # Anna auch ins zweite Abo aufnehmen
    anna2 = Member(
        subscription_id=seed["other_sub"].id,
        email="anna@example.com",
        name="Anna",
        password_hash="",
    )
    db.add(anna2)
    db.commit()
    db.refresh(anna2)

    csrf = member_login(client)  # loggt in die erste Mitgliedschaft ein
    resp = client.get("/member/dashboard")
    assert "Beachvolleyball" in resp.text
    assert "→ Fußball" in resp.text  # Umschalter sichtbar

    resp = client.post(
        f"/member/switch/{anna2.id}",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "Gewechselt" in resp.headers["location"]
    resp = client.get("/member/dashboard")
    assert "Fußball" in resp.text  # jetzt im zweiten Abo
    assert "→ Beachvolleyball" in resp.text  # zurückwechseln möglich

    # Fremde Mitgliedschaft (andere E-Mail) → abgelehnt
    resp = client.post(
        f"/member/switch/{seed['outsider'].id}",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "nicht%20gefunden" in resp.headers["location"]
