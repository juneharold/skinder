"""Validated operational configuration shared by every entrypoint."""

from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SKINDER_", env_file=".env", extra="ignore",
        populate_by_name=True, hide_input_in_errors=True,
    )

    env: Literal["development", "production", "test"] = "development"
    host: str = "0.0.0.0"
    port: int = Field(8742, validation_alias="PORT", gt=0, le=65535)
    app_origin: str = Field("http://127.0.0.1:8742", validation_alias="APP_ORIGIN")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    shutdown_grace_seconds: float = Field(30, gt=0)
    database_url: SecretStr = Field(SecretStr(""), validation_alias="DATABASE_URL")
    db_pool_size: int = Field(5, gt=0)
    db_max_overflow: int = Field(0, ge=0)
    db_pool_timeout_seconds: float = Field(10, gt=0)
    db_connect_timeout_seconds: int = Field(10, gt=0)

    github_client_id: str = Field("", validation_alias="GITHUB_CLIENT_ID")
    github_client_secret: SecretStr = Field(SecretStr(""), validation_alias="GITHUB_CLIENT_SECRET")
    session_secret: SecretStr = Field(SecretStr(""), validation_alias="SESSION_SECRET")
    session_ttl_seconds: int = Field(2592000, gt=0)
    oauth_http_timeout_seconds: float = Field(10, gt=0)

    letsur_api_key: SecretStr = Field(SecretStr(""), validation_alias="LETSUR_API_KEY")
    letsur_base_url: str = Field("https://gw.letsur.ai/v1", validation_alias="LETSUR_BASE_URL")
    letsur_model: str = Field("gpt-6.1-sol", validation_alias="LETSUR_MODEL", min_length=1)
    letsur_timeout_seconds: float = Field(120, gt=0)
    max_prompt_bytes: int = Field(65536, ge=8192)
    max_output_tokens: int = Field(8192, gt=0)
    daily_budget_units: Decimal = Field(Decimal(5), gt=0)
    matches_per_user_per_day: int = Field(10, gt=0)
    max_concurrent_jobs: int = Field(4, gt=0)
    worker_poll_seconds: float = Field(1, gt=0)
    job_timeout_seconds: float = Field(180, gt=0)
    queue_timeout_seconds: float = Field(600, gt=0)
    job_lease_seconds: float = Field(30, gt=0)
    job_heartbeat_seconds: float = Field(10, gt=0)
    max_job_claims: int = Field(3, gt=0)

    source_http_timeout_seconds: float = Field(10, gt=0)
    source_enrichment_timeout_seconds: float = Field(20, gt=0)
    source_concurrency: int = Field(4, gt=0)
    source_max_bytes: int = Field(262144, gt=0)
    source_excerpt_chars: int = Field(4000, gt=0)
    source_cache_ttl_seconds: int = Field(604800, gt=0)
    embedding_model_dir: Path = Path("/opt/skinder/models/e5-small")
    embedding_threads: int = Field(4, gt=0)
    embedding_batch_size: int = Field(64, gt=0)
    sync_keywords: list[str] = Field(default_factory=lambda: [
        "coding", "testing", "design", "data", "documentation", "automation",
    ], min_length=1)
    sync_max_requests: int = Field(12, gt=0)
    sync_daily_requests: int = Field(48, gt=0, le=50)
    sync_http_timeout_seconds: float = Field(30, gt=0)
    sync_min_interval_seconds: float = Field(6, ge=6)

    @field_validator("sync_keywords")
    @classmethod
    def valid_sync_keywords(cls, values):
        keywords = list(dict.fromkeys(value.strip() for value in values))
        if any(len(value) > 200 for value in keywords):
            raise ValueError("SKINDER_SYNC_KEYWORDS queries must be at most 200 characters")
        if any(not any(char.isalnum() for char in value) or "*" in value for value in keywords):
            raise ValueError("SKINDER_SYNC_KEYWORDS must contain concrete keyword queries")
        return keywords

    @model_validator(mode="after")
    def consistent_configuration(self):
        origin = urlsplit(self.app_origin)
        if (origin.scheme not in {"http", "https"} or not origin.hostname
                or origin.username or origin.password or origin.query or origin.fragment
                or origin.path not in {"", "/"}):
            raise ValueError("APP_ORIGIN must be an HTTP(S) origin without a path or credentials")
        self.app_origin = self.app_origin.rstrip("/")
        if self.env == "production" and origin.scheme != "https":
            raise ValueError("APP_ORIGIN must use HTTPS in production")
        provider = urlsplit(self.letsur_base_url)
        if (provider.scheme != "https" or not provider.hostname or provider.username
                or provider.password or provider.query or provider.fragment):
            raise ValueError("LETSUR_BASE_URL must be an HTTPS URL without credentials")
        if self.job_heartbeat_seconds >= self.job_lease_seconds:
            raise ValueError("SKINDER_JOB_HEARTBEAT_SECONDS must be shorter than the lease")
        value = self.database_url.get_secret_value()
        if value:
            try:
                parsed = make_url(value)
                if parsed.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
                    raise ValueError
            except (ArgumentError, ValueError):
                raise ValueError("DATABASE_URL must be a PostgreSQL connection URL") from None
        return self

    @property
    def secure_cookies(self) -> bool:
        return self.app_origin.startswith("https://")

    def require(self, role: Literal["web", "worker", "ingest", "migrate"]) -> None:
        required = {"DATABASE_URL": self.database_url.get_secret_value()}
        if role == "web":
            required.update({
                "GITHUB_CLIENT_ID": self.github_client_id,
                "GITHUB_CLIENT_SECRET": self.github_client_secret.get_secret_value(),
                "SESSION_SECRET": self.session_secret.get_secret_value(),
            })
        if role == "worker":
            required["LETSUR_API_KEY"] = self.letsur_api_key.get_secret_value()
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError("Missing settings: " + ", ".join(missing))
        if role == "web" and len(self.session_secret.get_secret_value()) < 32:
            raise ValueError("SESSION_SECRET must contain at least 32 characters")


def load_settings(role: Literal["web", "worker", "ingest", "migrate"]) -> Settings:
    try:
        settings = Settings()
        settings.require(role)
        return settings
    except ValidationError as exc:
        fields = sorted({".".join(map(str, error["loc"])) or "configuration"
                         for error in exc.errors()})
        raise SystemExit("Invalid settings: " + ", ".join(fields)) from None
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
