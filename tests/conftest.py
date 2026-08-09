"""Test fixtures: isolated temp SQLite DB, TestClient, seeded data."""

import os
import tempfile

# Must be set before any app import (settings load at import time)
_tmpdir = tempfile.mkdtemp(prefix="sportabo-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"
os.environ["DATA_DIR"] = _tmpdir
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production-abc123"
os.environ["ADMIN_PASSWORD"] = "test-admin-pw"
os.environ["APP_ENV"] = "dev"
os.environ["COOKIE_SECURE"] = "false"
os.environ["SMTP_HOST"] = ""
os.environ["ENABLE_SCHEDULER"] = "false"

import secrets
from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password, reset_rate_limits
from app.database import SessionLocal, engine
from app.main import app
from app.models.models import (
    Base,
    Event,
    LoginToken,
    Member,
    Subscription,
    utcnow,
)

ADMIN_PW = "test-admin-pw"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_db():
    """Fresh schema and empty rate limits for every test."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    reset_rate_limits()
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def seed(db):
    """One subscription, one future event, two members (one in a 2nd abo)."""
    sub = Subscription(
        name="Beachvolleyball",
        weekday=2,
        start_time=time(18, 0),
        duration_minutes=120,
        start_date=date.today() - timedelta(days=30),
        end_date=date.today() + timedelta(days=30),
        # Totals for the whole Abo; the seed creates 2 active events →
        # event budgets: abo 16/2 = 8.00 €, normal 20/2 = 10.00 €.
        # Per-person shares divide these budgets by the participants.
        default_price=Decimal("20.00"),
        abo_price=Decimal("16.00"),
        max_participants=4,
        paypal_address="pay@example.com",
    )
    other_sub = Subscription(
        name="Fußball",
        weekday=4,
        start_time=time(19, 0),
        start_date=date.today() - timedelta(days=30),
        end_date=date.today() + timedelta(days=30),
    )
    db.add_all([sub, other_sub])
    db.flush()

    event = Event(
        subscription_id=sub.id,
        date=date.today() + timedelta(days=3),
        start_time=time(18, 0),
        end_time=time(20, 0),
        max_participants=4,
        min_participants=1,
        abo_budget=Decimal("8.00"),
        normal_budget=Decimal("10.00"),
    )
    past_event = Event(
        subscription_id=sub.id,
        date=date.today() - timedelta(days=3),
        start_time=time(18, 0),
        end_time=time(20, 0),
        max_participants=4,
        min_participants=1,
        abo_budget=Decimal("8.00"),
        normal_budget=Decimal("10.00"),
    )
    member = Member(
        subscription_id=sub.id,
        email="anna@example.com",
        name="Anna",
        password_hash=hash_password("secret123"),
        credit=Decimal("20.00"),
    )
    super_member = Member(
        subscription_id=sub.id,
        email="sina@example.com",
        name="Sina",
        password_hash=hash_password("secret123"),
        is_super=True,
    )
    outsider = Member(
        subscription_id=other_sub.id,
        email="bernd@example.com",
        name="Bernd",
        password_hash=hash_password("secret123"),
    )
    db.add_all([event, past_event, member, super_member, outsider])
    db.commit()
    for obj in (sub, other_sub, event, past_event, member, super_member, outsider):
        db.refresh(obj)
    return {
        "sub": sub,
        "other_sub": other_sub,
        "event": event,
        "past_event": past_event,
        "member": member,
        "super_member": super_member,
        "outsider": outsider,
    }


def get_csrf(client: TestClient, url: str) -> str:
    """Visit a page so the csrf cookie gets set, return the token."""
    resp = client.get(url)
    assert resp.status_code == 200, f"GET {url} -> {resp.status_code}"
    return client.cookies.get("csrf_token")


def admin_login(client: TestClient) -> str:
    csrf = get_csrf(client, "/admin/login")
    resp = client.post(
        "/admin/login",
        data={"password": ADMIN_PW, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"] == "/admin/dashboard"
    return csrf


def make_login_token(email: str) -> str:
    """Create a valid login token directly in the DB (bypasses email)."""
    db = SessionLocal()
    try:
        member = db.query(Member).filter(Member.email == email).first()
        assert member, f"no member with email {email}"
        token = LoginToken(
            token=secrets.token_urlsafe(32),
            member_id=member.id,
            expires_at=utcnow() + timedelta(minutes=15),
        )
        db.add(token)
        db.commit()
        return token.token
    finally:
        db.close()


def member_login(client: TestClient, email="anna@example.com") -> str:
    """Log a member in via login token; returns the CSRF token."""
    csrf = get_csrf(client, "/member/login")
    token = make_login_token(email)
    resp = client.get(f"/member/login/t/{token}", follow_redirects=False)
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"] == "/member/dashboard", resp.headers["location"]
    return csrf
