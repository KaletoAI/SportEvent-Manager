"""Jinja2 environment with FastAPI-compatible TemplateResponse.

Every rendered page gets a `csrf_token` in its context (double-submit
cookie pattern); the cookie is set on the response when missing.
"""

import secrets
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import settings

_templates_dir = Path(__file__).resolve().parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_templates_dir)),
    autoescape=select_autoescape(["html", "xml"]),
)

_WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
WEEKDAYS_DE_LONG = [
    "Montag", "Dienstag", "Mittwoch", "Donnerstag",
    "Freitag", "Samstag", "Sonntag",
]


def format_date(value: date | datetime | None) -> str:
    """2026-07-15 → 'Mi, 15.07.2026'"""
    if value is None:
        return "—"
    if isinstance(value, datetime):
        value = value.date()
    return f"{_WEEKDAYS_DE[value.weekday()]}, {value.strftime('%d.%m.%Y')}"


def format_time(value: time | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%H:%M")


def format_euro(value: Decimal | float | int | None) -> str:
    if value is None:
        return "—"
    quantized = Decimal(str(value)).quantize(Decimal("0.01"))
    return f"{quantized:.2f} €".replace(".", ",")


_env.filters["date_de"] = format_date
_env.filters["time_de"] = format_time
_env.filters["euro"] = format_euro
_env.globals["weekdays_de"] = WEEKDAYS_DE_LONG


def _static_version() -> int:
    """Cache-buster for the stylesheet: changes whenever the file does."""
    css = Path(__file__).resolve().parent / "static" / "style.css"
    try:
        return int(css.stat().st_mtime)
    except OSError:
        return 0


_env.globals["static_version"] = _static_version()

templates = _env  # Jinja2 environment


def TemplateResponse(
    template_name: str,
    context: dict[str, Any],
    status_code: int = 200,
) -> HTMLResponse:
    """Render a template; inject csrf_token and set its cookie if missing."""
    request = context.get("request")
    csrf_token = None
    csrf_cookie_missing = False
    if request is not None:
        csrf_token = request.cookies.get("csrf_token")
        if not csrf_token:
            csrf_token = secrets.token_urlsafe(32)
            csrf_cookie_missing = True
        context.setdefault("csrf_token", csrf_token)

    template = _env.get_template(template_name)
    html = template.render(**context)
    response = HTMLResponse(content=html, status_code=status_code)
    if csrf_cookie_missing:
        # Readable by the page's own forms only via template injection;
        # not httponly so it survives without server-side state.
        response.set_cookie(
            "csrf_token",
            csrf_token,
            max_age=86400 * 30,
            samesite="lax",
            secure=settings.cookie_secure,
        )
    return response
