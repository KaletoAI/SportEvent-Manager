"""Hilfe-Seiten: Handbuch und Kurzreferenz unter /hilfe.

Die beiden HTML-Dokumente werden aus `docs/*.src.html` gebaut
(`scripts/build_manual.py`) und liegen fertig unter `app/help/`. Sie werden
über Routen ausgeliefert statt über den statischen Mount, weil das Handbuch
eine Anmeldung voraussetzt — alles unter `/static` wäre öffentlich.

Die Kurzreferenz bleibt öffentlich: Gäste kommen ohne Konto über den
Buchungslink und brauchen die Gast-Anleitung.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.auth import SESSION_COOKIE, get_session
from app.database import get_db
from app.templates import TemplateResponse

router = APIRouter()

HELP_DIR = Path(__file__).resolve().parent.parent / "help"

DOCS = {
    "handbuch": {
        "file": "usermanual.html",
        "title": "Handbuch",
        "protected": True,
    },
    "kurzreferenz": {
        "file": "kurzreferenz.html",
        "title": "Kurzreferenz",
        "protected": False,
    },
}


def _path(key: str) -> Path:
    return HELP_DIR / DOCS[key]["file"]


def require_any_session(
    request: Request, db: Session = Depends(get_db)
) -> None:
    """Irgendeine gültige Anmeldung — Mitglied oder Admin."""
    if not get_session(db, request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/member/login"},
        )


@router.get("")
@router.get("/")
async def help_index(request: Request, db: Session = Depends(get_db)):
    """Übersicht mit beiden Dokumenten."""
    return TemplateResponse(
        "help.html",
        {
            "request": request,
            "logged_in": bool(
                get_session(db, request.cookies.get(SESSION_COOKIE))
            ),
            "has_manual": _path("handbuch").exists(),
            "has_quickref": _path("kurzreferenz").exists(),
        },
    )


@router.get("/kurzreferenz")
async def quickref():
    """Öffentlich — auch für Gäste ohne Konto."""
    path = _path("kurzreferenz")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Kurzreferenz nicht gefunden")
    return FileResponse(path, media_type="text/html")


@router.get("/handbuch", dependencies=[Depends(require_any_session)])
async def manual():
    """Nur für Angemeldete: enthält auch das Admin-Kapitel."""
    path = _path("handbuch")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Handbuch nicht gefunden")
    return FileResponse(path, media_type="text/html")
