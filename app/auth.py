"""Authentication: server-side sessions, rate limiting and CSRF verification."""

import hmac
import secrets
import time as time_module
from collections import defaultdict, deque
from datetime import timedelta
from typing import Optional

from fastapi import Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.models import LoginToken, Member, UserSession, utcnow
from app.web import client_ip

SESSION_COOKIE = "session"
CSRF_COOKIE = "csrf_token"


# ── Sessions ────────────────────────────────────────────────────────────────


def create_session(
    db: Session, member_id: Optional[str] = None, is_admin: bool = False
) -> UserSession:
    session = UserSession(
        token=secrets.token_urlsafe(48),
        member_id=member_id,
        is_admin=is_admin,
        expires_at=utcnow() + timedelta(days=settings.session_max_age_days),
    )
    db.add(session)
    db.commit()
    return session


def get_session(db: Session, token: Optional[str]) -> Optional[UserSession]:
    """Return a valid session or None; expired sessions are deleted.

    Sliding expiration: every use extends the session to the full
    max age again (write-throttled to about once a day), so devices in
    active use stay logged in indefinitely."""
    if not token:
        return None
    session = db.query(UserSession).filter(UserSession.token == token).first()
    if not session:
        return None
    now = utcnow()
    if session.expires_at < now:
        db.delete(session)
        db.commit()
        return None
    full = timedelta(days=settings.session_max_age_days)
    if session.expires_at < now + full - timedelta(days=1):
        session.expires_at = now + full
        db.commit()
    return session


def destroy_session(db: Session, token: Optional[str]) -> None:
    if not token:
        return
    db.query(UserSession).filter(UserSession.token == token).delete()
    db.commit()


def purge_expired(db: Session) -> int:
    """Delete expired sessions and login tokens (scheduler housekeeping —
    otherwise they are only removed when someone tries to use them)."""
    now = utcnow()
    n = db.query(UserSession).filter(UserSession.expires_at < now).delete()
    n += db.query(LoginToken).filter(LoginToken.expires_at < now).delete()
    db.commit()
    return n


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=86400 * settings.session_cookie_days,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)


def _login_redirect(url: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_302_FOUND, headers={"Location": url}
    )


def require_admin(request: Request, db: Session = Depends(get_db)) -> UserSession:
    """FastAPI dependency: valid admin session or redirect to login."""
    session = get_session(db, request.cookies.get(SESSION_COOKIE))
    if not session or not session.is_admin:
        raise _login_redirect("/admin/login")
    return session


def get_current_member(request: Request, db: Session) -> Optional[Member]:
    session = get_session(db, request.cookies.get(SESSION_COOKIE))
    if not session or session.is_admin or not session.member_id:
        return None
    member = db.query(Member).filter(Member.id == session.member_id).first()
    if member and not member.is_active:
        return None
    return member


def require_member(request: Request, db: Session = Depends(get_db)) -> Member:
    """FastAPI dependency: valid member session or redirect to login."""
    member = get_current_member(request, db)
    if not member:
        raise _login_redirect("/member/login")
    return member


def require_super(member: Member = Depends(require_member)) -> Member:
    """FastAPI dependency: member with super-member rights."""
    if not member.is_super:
        raise HTTPException(status_code=403, detail="Nur für Super-Mitglieder")
    return member


# ── Rate limiting (in-memory, resets on restart) ───────────────────────────

_attempts: dict[str, deque] = defaultdict(deque)


def check_rate_limit(
    key: str, max_attempts: int, window_seconds: int, detail: str
) -> None:
    """Raise 429 when `key` was used max_attempts times within the window."""
    now = time_module.monotonic()
    attempts = _attempts[key]
    while attempts and now - attempts[0] > window_seconds:
        attempts.popleft()
    if len(attempts) >= max_attempts:
        raise HTTPException(status_code=429, detail=detail)
    attempts.append(now)


def check_login_rate_limit(request: Request, scope: str) -> None:
    """Raise 429 when too many recent login attempts from this IP."""
    check_rate_limit(
        f"{scope}:{client_ip(request)}",
        settings.login_max_attempts,
        settings.login_window_seconds,
        "Zu viele Login-Versuche. Bitte später erneut versuchen.",
    )


def reset_rate_limits() -> None:
    """For tests."""
    _attempts.clear()


# ── CSRF (double-submit cookie) ────────────────────────────────────────────


def verify_csrf(
    request: Request,
    csrf_token: Optional[str] = Form(None),
) -> None:
    """Router-level dependency: on state-changing requests the form field
    must match the csrf cookie (set by TemplateResponse on first render)."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie or not csrf_token or not hmac.compare_digest(cookie, csrf_token):
        raise HTTPException(status_code=400, detail="CSRF-Prüfung fehlgeschlagen")
