"""FastAPI application entry point."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth import verify_csrf
from app.config import settings
from app.database import engine
from app.models.models import Base
from app.routes import admin, guest, member
from app.routes import help as help_routes
from app.scheduler import scheduler_loop
from app.schema_upgrade import upgrade

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if settings.has_insecure_defaults:
    if settings.app_env == "production":
        raise RuntimeError(
            "Refusing to start: SECRET_KEY and/or ADMIN_PASSWORD still have "
            "insecure default values. Set them via environment or .env."
        )
    logger.warning(
        "SECRET_KEY/ADMIN_PASSWORD are insecure defaults — dev use only!"
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if settings.enable_scheduler:
        task = asyncio.create_task(scheduler_loop())
    yield
    if task:
        task.cancel()


app = FastAPI(
    title="SportAbo Manager", docs_url=None, redoc_url=None, lifespan=lifespan
)

# Create missing tables, then apply in-place column upgrades
Base.metadata.create_all(bind=engine)
upgrade(engine)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        if settings.cookie_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000"
            )
        return response


app.add_middleware(SecurityHeadersMiddleware)

# Static files
static_dir = Path(__file__).resolve().parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Routers — verify_csrf checks the double-submit token on every non-GET
csrf = [Depends(verify_csrf)]
app.include_router(admin.router, prefix="/admin", tags=["admin"], dependencies=csrf)
app.include_router(member.router, prefix="/member", tags=["member"], dependencies=csrf)
app.include_router(guest.router, prefix="/g", tags=["guest"], dependencies=csrf)
app.include_router(
    help_routes.router, prefix="/hilfe", tags=["help"], dependencies=csrf
)


@app.get("/")
async def root():
    return RedirectResponse(url="/member/login")


@app.get("/health")
async def health():
    return {"status": "ok"}
