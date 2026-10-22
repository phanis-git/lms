from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError, forbidden
from app.core.security import constant_time_equals, hash_token
from app.models import User, UserSession
from app.models.enums import Role, UserStatus

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


def public_origin(request: Request) -> str:
    """Origin the browser used to reach us (honours the NGINX proxy headers)."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto.split(',')[0].strip()}://{host.split(',')[0].strip()}"


def _load_session(request: Request, db: Session) -> UserSession | None:
    s = get_settings()
    raw = request.cookies.get(s.session_cookie_name)
    if not raw:
        return None
    sess = db.get(UserSession, hash_token(raw))
    now = datetime.now(UTC)
    if not sess or sess.revoked or sess.expires_at <= now:
        return None
    if sess.user.status != UserStatus.ACTIVE:
        return None
    if not sess.last_seen_at or now - sess.last_seen_at > timedelta(minutes=5):
        sess.last_seen_at = now
        db.commit()
    return sess


def _check_csrf(request: Request, sess: UserSession) -> None:
    if request.method in SAFE_METHODS:
        return
    s = get_settings()
    header = request.headers.get(s.csrf_header_name, "")
    if not header or not constant_time_equals(header, sess.csrf_token):
        raise AppError(403, "csrf_failed", "Missing or invalid CSRF token. Refresh the page and try again.")


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    sess = _load_session(request, db)
    if not sess:
        return None
    _check_csrf(request, sess)
    request.state.session = sess
    return sess.user


def get_current_user(user: User | None = Depends(get_optional_user)) -> User:
    if not user:
        raise AppError(401, "unauthorized", "Please sign in to continue")
    return user


def require_roles(*roles: Role) -> Callable[..., User]:
    def dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise forbidden()
        return user

    return dep


require_admin = require_roles(Role.ADMIN)
require_author = require_roles(Role.ADMIN, Role.INSTRUCTOR)
require_student = require_roles(Role.STUDENT)
