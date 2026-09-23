"""Shared helpers for the route modules: flash redirects and public URLs.

Flash messages travel as `?msg=…&mt=…&sig=…`. The signature (HMAC over
type + text with SECRET_KEY) lets `TemplateResponse` drop messages that
did not come from one of our own redirects — otherwise anyone could put
arbitrary text on the login page via a crafted link (phishing).
"""

import hashlib
import hmac
from datetime import time
from typing import Optional
from urllib.parse import quote, urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse

from app.config import settings


def flash_signature(msg: str, mt: str) -> str:
    return hmac.new(
        settings.secret_key.encode(), f"{mt}:{msg}".encode(), hashlib.sha256
    ).hexdigest()[:16]


def flash_redirect(msg: str, url: str, mt: str = "success") -> RedirectResponse:
    """Redirect with a signed flash message (mt = 'success' | 'error')."""
    query = urlencode(
        {"msg": msg, "mt": mt, "sig": flash_signature(msg, mt)}, quote_via=quote
    )
    sep = "&" if "?" in url else "?"
    return RedirectResponse(url=f"{url}{sep}{query}", status_code=302)


def flash_is_authentic(request: Request, msg: str) -> bool:
    """True unless `msg` came from the query string without a valid signature."""
    if msg != request.query_params.get("msg"):
        return True  # set by the route itself, not taken from the URL
    mt = request.query_params.get("mt", "success")
    sig = request.query_params.get("sig", "")
    return hmac.compare_digest(sig, flash_signature(msg, mt))


def public_base_url(request: Request) -> str:
    """Base URL for links in emails and share links: the configured
    BASE_URL, else derived from the request (dev only — the Host header
    is client-controlled)."""
    if settings.base_url:
        return settings.base_url.rstrip("/") + "/"
    return str(request.base_url)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def parse_clock(
    value: Optional[str], hour: Optional[int] = None, minute: Optional[int] = None
) -> time:
    """Time of day from an <input type="time"> ("HH:MM"), falling back to
    separate hour/minute fields. Raises ValueError when neither is usable."""
    if value:
        return time.fromisoformat(value[:5])
    if hour is None:
        raise ValueError("no time given")
    return time(hour=hour, minute=minute or 0)
