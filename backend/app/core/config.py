from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "DevOps Freshers"
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://lms:lms@localhost:5432/lms"
    frontend_url: str = "http://localhost:8080"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:4200"])

    session_cookie_name: str = "df_session"
    csrf_cookie_name: str = "XSRF-TOKEN"
    csrf_header_name: str = "X-XSRF-TOKEN"
    session_ttl_hours: int = 24 * 7
    cookie_secure: bool = True
    require_email_verification: bool = True

    email_verification_ttl_hours: int = 48
    password_reset_ttl_minutes: int = 30

    # Email
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    email_from: str = "DevOps Freshers <no-reply@devopsfreshers.local>"
    approval_notification_email: str = "sivaphanikumar.tech@gmail.com"

    # Object storage (S3 compatible)
    s3_endpoint_url: str | None = None
    s3_public_endpoint_url: str | None = None
    s3_presign_same_origin: bool = False
    s3_region: str = "ap-south-1"
    s3_bucket: str = "devops-freshers-media"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_create_bucket: bool = False
    upload_url_ttl_seconds: int = 3600
    playback_url_ttl_seconds: int = 900
    max_video_upload_mb: int = 4096
    max_resource_upload_mb: int = 200
    max_image_upload_mb: int = 5
    multipart_part_size_mb: int = 16

    # CloudFront signed URLs (optional, production)
    cloudfront_domain: str | None = None
    cloudfront_key_pair_id: str | None = None
    cloudfront_private_key_path: str | None = None

    # Learning rules
    video_completion_threshold: float = 0.9

    # Rate limiting
    auth_rate_limit_per_minute: int = 10

    # Bootstrap admin (optional; only used by the CLI / when no admin exists)
    initial_admin_email: str | None = None
    initial_admin_password: str | None = None
    initial_admin_name: str = "Platform Admin"

    log_level: str = "INFO"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
