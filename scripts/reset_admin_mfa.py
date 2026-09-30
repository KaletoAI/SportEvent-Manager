"""Admin-MFA zurücksetzen (Handy verloren): löscht das TOTP-Geheimnis und
beendet alle Admin-Sitzungen. Danach führt der nächste Admin-Login mit
Passwort wieder zur Einrichtung (/admin/mfa/setup).

Im Produktions-Container (als User sportabo, damit .env und DB passen):
    cd /opt/sportabo && runuser -u sportabo -- venv/bin/python scripts/reset_admin_mfa.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import mfa  # noqa: E402
from app.database import SessionLocal  # noqa: E402

db = SessionLocal()
try:
    was_enrolled = mfa.is_enrolled(db)
    mfa.reset(db)
finally:
    db.close()
print("Admin-MFA zurückgesetzt." if was_enrolled else "Es war keine Admin-MFA eingerichtet.")
print("Alle Admin-Sitzungen beendet. Nächster Login: Passwort, dann Einrichtung.")
