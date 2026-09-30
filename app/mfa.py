"""Admin MFA: TOTP (authenticator app) as second factor for the admin login.

The secret lives in `app_settings` (not in the cookie, not in .env) and is
set up in the app: after a password-only login the admin scans a QR code
and confirms with a code. In production MFA is mandatory — until it is set
up, admin sessions only reach the setup page. Used codes can't be replayed
(the last accepted 30-second step is stored). Lost phone: run
`scripts/reset_admin_mfa.py` inside the container.
"""

import hmac
import io
import time
from typing import Optional

import pyotp
import segno
from sqlalchemy.orm import Session

from app.config import settings
from app.models.models import AppSetting, UserSession

SECRET_KEY = "admin_totp_secret"
PENDING_KEY = "admin_totp_pending"
LAST_STEP_KEY = "admin_totp_last_step"
ISSUER = "SportAbo Manager"
ACCOUNT = "Admin"


def _get(db: Session, key: str) -> Optional[str]:
    row = db.get(AppSetting, key)
    return row.value if row and row.value else None


def _set(db: Session, key: str, value: Optional[str]) -> None:
    """Caller commits."""
    row = db.get(AppSetting, key)
    if value is None:
        if row:
            db.delete(row)
    elif row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))


def is_enrolled(db: Session) -> bool:
    return _get(db, SECRET_KEY) is not None


def required(db: Session) -> bool:
    """MFA is mandatory in production, elsewhere once it is set up."""
    return settings.app_env == "production" or is_enrolled(db)


def _matching_step(secret: str, code: str) -> Optional[int]:
    """Time step the code belongs to (±1 step for clock drift), or None."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != 6 or not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    now_step = int(time.time()) // totp.interval
    for step in (now_step - 1, now_step, now_step + 1):
        if hmac.compare_digest(totp.at(step * totp.interval), code):
            return step
    return None


def _accept(db: Session, secret: str, code: str) -> bool:
    """Check the code and burn its time step (no replay). Caller commits."""
    step = _matching_step(secret, code)
    if step is None:
        return False
    last = _get(db, LAST_STEP_KEY)
    if last is not None and step <= int(last):
        return False
    _set(db, LAST_STEP_KEY, str(step))
    return True


def verify(db: Session, code: str) -> bool:
    """Login check against the enrolled secret."""
    secret = _get(db, SECRET_KEY)
    if not secret:
        return False
    ok = _accept(db, secret, code)
    db.commit()
    return ok


def pending_secret(db: Session) -> str:
    """Secret shown on the setup page; stays the same until confirmed."""
    secret = _get(db, PENDING_KEY)
    if not secret:
        secret = pyotp.random_base32()
        _set(db, PENDING_KEY, secret)
        db.commit()
    return secret


def qr_svg(secret: str) -> bytes:
    uri = pyotp.TOTP(secret).provisioning_uri(name=ACCOUNT, issuer_name=ISSUER)
    buf = io.BytesIO()
    segno.make(uri, error="m").save(buf, kind="svg", scale=6, border=2)
    return buf.getvalue()


def confirm_setup(db: Session, code: str, session: UserSession) -> bool:
    """Activate the pending secret if the code matches. All other admin
    sessions (password-only) end; the current one counts as verified."""
    secret = _get(db, PENDING_KEY)
    if not secret or not _accept(db, secret, code):
        db.rollback()
        return False
    _set(db, SECRET_KEY, secret)
    _set(db, PENDING_KEY, None)
    db.query(UserSession).filter(
        UserSession.is_admin.is_(True), UserSession.id != session.id
    ).delete()
    session.mfa_verified = True
    db.commit()
    return True


def reset(db: Session) -> None:
    """Emergency reset (lost phone): remove MFA and end all admin sessions."""
    for key in (SECRET_KEY, PENDING_KEY, LAST_STEP_KEY):
        _set(db, key, None)
    db.query(UserSession).filter(UserSession.is_admin.is_(True)).delete()
    db.commit()
