"""Application configuration via environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_SECRET_KEY_DEFAULT = "change-me-in-production-use-a-long-random-string"
INSECURE_ADMIN_PASSWORD_DEFAULT = "admin123"


class Settings(BaseSettings):
    # Empty = default SQLite file under data_dir (see app/database.py)
    database_url: str = ""
    secret_key: str = INSECURE_SECRET_KEY_DEFAULT
    admin_password: str = INSECURE_ADMIN_PASSWORD_DEFAULT
    admin_email: str = "admin@example.com"

    # "dev" or "production". In production the app refuses to start with
    # insecure default credentials.
    app_env: str = "dev"

    # Session: server-side sliding window — every use extends the session
    # by session_max_age_days; only session_max_age_days of inactivity
    # logs a device out. The cookie itself lives session_cookie_days.
    session_max_age_days: int = 30
    session_cookie_days: int = 365

    # Set cookies with the Secure flag (requires HTTPS, i.e. behind the
    # reverse proxy in production).
    cookie_secure: bool = False

    # Login rate limiting (per client IP)
    login_max_attempts: int = 10
    login_window_seconds: int = 900

    # Magic-link login tokens
    login_token_minutes: int = 15

    # Public base URL for links in emails (e.g. https://sportabo.example.com
    # or http://<lan-ip>:8000 in dev). Empty = derive from the request.
    base_url: str = ""

    # Background scheduler (reminder mails, auto settlement)
    enable_scheduler: bool = True
    scheduler_interval_seconds: int = 900

    # Default SMTP (can be overridden per subscription)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_from: str = "noreply@sportabo.example.com"
    email_from_name: str = "SportAbo"

    data_dir: str = str(Path(__file__).resolve().parent.parent / "data")

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8"
    )

    @property
    def has_insecure_defaults(self) -> bool:
        return (
            self.secret_key == INSECURE_SECRET_KEY_DEFAULT
            or self.admin_password == INSECURE_ADMIN_PASSWORD_DEFAULT
        )


settings = Settings()
