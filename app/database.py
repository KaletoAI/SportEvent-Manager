"""Database engine and session management."""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


def _prepare_sqlite(url: str) -> dict:
    """Ensure the parent directory of a SQLite DB exists."""
    path_part = url.split("sqlite:///", 1)[1]
    if path_part and path_part != ":memory:":
        Path(path_part).parent.mkdir(parents=True, exist_ok=True)
    return {"check_same_thread": False}


database_url = settings.database_url or (
    f"sqlite:///{Path(settings.data_dir) / 'sportabo.db'}"
)

connect_args = {}
if database_url.startswith("sqlite:///"):
    connect_args = _prepare_sqlite(database_url)

engine = create_engine(
    database_url,
    echo=False,
    connect_args=connect_args,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Session:
    """FastAPI dependency that yields a DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
