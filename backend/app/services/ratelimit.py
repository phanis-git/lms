from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.errors import AppError


def enforce_rate_limit(db: Session, key: str, limit: int, window_seconds: int = 60) -> None:
    """Fixed-window counter stored in Postgres (works across multiple API containers, no Redis needed)."""
    now = datetime.now(UTC)
    window = datetime.fromtimestamp(int(now.timestamp()) // window_seconds * window_seconds, UTC)
    count = db.execute(
        text(
            "INSERT INTO rate_limit_counters (key, window_start, count) VALUES (:k, :w, 1) "
            "ON CONFLICT (key, window_start) DO UPDATE SET count = rate_limit_counters.count + 1 "
            "RETURNING count"
        ),
        {"k": key, "w": window},
    ).scalar_one()
    db.commit()
    if count > limit:
        raise AppError(429, "rate_limited", "Too many requests. Please wait a minute and try again.")


def purge_old_counters(db: Session) -> None:
    db.execute(text("DELETE FROM rate_limit_counters WHERE window_start < now() - interval '1 hour'"))
    db.commit()
