"""System clock: single source for 'today' across the app.

The admin can temporarily override the current date in the UI (for
testing settlement flows etc.). The override is stored in the DB
(app_settings table) so it survives reloads and is visible to every
request — remember to reset it after testing.

All date-based business logic (past checks, upcoming filters, settle
blockers) MUST use clock.today(db) instead of date.today(). Ledger
timestamps (created_at etc.) intentionally keep real time.
"""

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from app.models.models import AppSetting

DATE_OVERRIDE_KEY = "date_override"


def get_override(db: Session) -> Optional[date]:
    row = db.get(AppSetting, DATE_OVERRIDE_KEY)
    if row and row.value:
        try:
            return date.fromisoformat(row.value)
        except ValueError:
            return None
    return None


def today(db: Session) -> date:
    """Effective current date: admin override if set, else the real date."""
    return get_override(db) or date.today()


def now(db: Session) -> "datetime":
    """Effective current datetime (naive local, matching event times):
    with an override the date is replaced, the time of day stays real."""
    from datetime import datetime

    real = datetime.now()
    override = get_override(db)
    if override:
        return datetime.combine(override, real.time())
    return real


def set_override(db: Session, value: Optional[date]) -> None:
    """Set or clear (value=None) the date override."""
    row = db.get(AppSetting, DATE_OVERRIDE_KEY)
    if value is None:
        if row:
            db.delete(row)
    elif row:
        row.value = value.isoformat()
    else:
        db.add(AppSetting(key=DATE_OVERRIDE_KEY, value=value.isoformat()))
    db.commit()
