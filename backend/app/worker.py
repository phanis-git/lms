"""Background worker: delivers the email outbox and extracts video metadata (duration, thumbnail).

Run with `python -m app.worker`. Safe to run multiple replicas: rows are claimed with SKIP LOCKED.
"""

import json
import logging
import shutil
import subprocess  # noqa: S404
import tempfile
import time
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import MediaAsset
from app.models.enums import MediaKind, MediaStatus
from app.services import storage
from app.services.notify import deliver_pending_emails
from app.services.ratelimit import purge_old_counters

log = logging.getLogger("worker")


def _probe(url: str) -> float | None:
    out = subprocess.run(  # noqa: S603
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", url],
        capture_output=True,
        timeout=120,
        check=False,
    )
    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return None


def _thumbnail(url: str, at: float) -> bytes | None:
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        r = subprocess.run(  # noqa: S603
            ["ffmpeg", "-y", "-v", "error", "-ss", str(at), "-i", url, "-frames:v", "1", "-vf", "scale=640:-2", f.name],
            capture_output=True,
            timeout=120,
            check=False,
        )
        return f.read() if r.returncode == 0 else None


def process_videos(limit: int = 3) -> int:
    if not shutil.which("ffprobe"):
        return 0
    n = 0
    with SessionLocal() as db:
        assets = db.scalars(
            select(MediaAsset)
            .where(
                MediaAsset.kind == MediaKind.VIDEO,
                MediaAsset.status == MediaStatus.READY,
                MediaAsset.processed_at.is_(None),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for a in assets:
            url = storage.signed_get_url(a.storage_key, None, ttl=600)
            try:
                a.duration_seconds = _probe(url) or a.duration_seconds
                if a.duration_seconds and (img := _thumbnail(url, min(5.0, a.duration_seconds / 2))):
                    key = a.storage_key.rsplit(".", 1)[0] + "-thumb.jpg"
                    import io

                    storage.put_object(key, io.BytesIO(img), "image/jpeg")
                    a.thumbnail_key = key
            except Exception:  # keep the asset playable even if metadata extraction fails
                log.exception("metadata extraction failed for %s", a.id)
            a.processed_at = datetime.now(UTC)
            n += 1
        db.commit()
    return n


def run_once() -> None:
    with SessionLocal() as db:
        sent = deliver_pending_emails(db, limit=50)
        if sent:
            log.info("delivered %s email(s)", sent)
    process_videos()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        storage.ensure_bucket()
    except Exception:
        log.exception("could not ensure bucket")
    last_purge = 0.0
    while True:
        try:
            run_once()
            if time.time() - last_purge > 3600:
                with SessionLocal() as db:
                    purge_old_counters(db)
                    db.commit()
                last_purge = time.time()
        except Exception:
            log.exception("worker iteration failed")
        time.sleep(5)


if __name__ == "__main__":
    main()
