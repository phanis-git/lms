from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import client_ip, get_current_user
from app.core.errors import AppError, bad_request, conflict
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    hash_token,
    needs_rehash,
    new_token,
    verify_password,
)
from app.models import EmailVerificationToken, PasswordResetToken, User, UserProfile, UserSession
from app.models.enums import Role, UserStatus
from app.schemas.auth import (
    ChangePasswordIn,
    EmailIn,
    LoginIn,
    RegisterIn,
    ResetPasswordIn,
    SessionOut,
    TokenIn,
)
from app.schemas.common import Message
from app.services.approvals import create_role_request
from app.services.audit import audit
from app.services.notify import notify
from app.services.ratelimit import enforce_rate_limit
from app.services.users import find_user_by_email, issue_email_verification, issue_password_reset, user_out

router = APIRouter(prefix="/auth", tags=["auth"])


def _limit(db: Session, request: Request, bucket: str, extra: str = "") -> None:
    s = get_settings()
    enforce_rate_limit(db, f"{bucket}:{client_ip(request)}", s.auth_rate_limit_per_minute)
    if extra:
        enforce_rate_limit(db, f"{bucket}:id:{extra.lower()}", s.auth_rate_limit_per_minute)


def _set_session_cookies(response: Response, raw: str, csrf: str) -> None:
    s = get_settings()
    max_age = s.session_ttl_hours * 3600
    response.set_cookie(
        s.session_cookie_name, raw, max_age=max_age, httponly=True, secure=s.cookie_secure, samesite="lax", path="/"
    )
    # Readable by the SPA (double-submit pattern); Angular sends it back as X-XSRF-TOKEN.
    response.set_cookie(
        s.csrf_cookie_name, csrf, max_age=max_age, httponly=False, secure=s.cookie_secure, samesite="lax", path="/"
    )


def _clear_session_cookies(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(s.session_cookie_name, path="/")
    response.delete_cookie(s.csrf_cookie_name, path="/")


@router.post("/register", response_model=Message, status_code=201)
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    _limit(db, request, "register")
    if find_user_by_email(db, body.email):
        raise conflict(
            "An account with this email already exists. Try signing in or resetting your password.", "email_taken"
        )
    # Everyone starts as a STUDENT. Elevated roles are only granted by an Admin approval.
    user = User(
        email=body.email.strip().lower(),
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    user.profile = UserProfile(phone=body.phone)
    db.add(user)
    db.flush()
    issue_email_verification(db, user)
    if body.requested_role in (Role.INSTRUCTOR, Role.ADMIN):
        create_role_request(db, user, body.requested_role, body.motivation, body.expertise)
    audit(
        db,
        user.id,
        "user.registered",
        "user",
        user.id,
        {"requested_role": body.requested_role.value},
        client_ip(request),
    )
    db.commit()
    msg = "Account created. Check your email to verify your address."
    if body.requested_role != Role.STUDENT:
        msg += f" Your {body.requested_role.value.lower()} request will be reviewed by an Admin."
    return Message(message=msg)


@router.post("/login", response_model=SessionOut)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    _limit(db, request, "login", body.email)
    s = get_settings()
    user = find_user_by_email(db, body.email)
    if not user:
        verify_password(DUMMY_PASSWORD_HASH, body.password)
        raise AppError(401, "invalid_credentials", "Incorrect email or password")
    if not verify_password(user.password_hash, body.password):
        audit(db, user.id, "auth.login_failed", "user", user.id, ip=client_ip(request))
        db.commit()
        raise AppError(401, "invalid_credentials", "Incorrect email or password")
    if user.status != UserStatus.ACTIVE:
        raise AppError(403, "account_suspended", "This account is suspended. Contact support for help.")
    if s.require_email_verification and not user.email_verified_at:
        raise AppError(403, "email_not_verified", "Please verify your email address before signing in.")
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    raw, csrf = new_token(), new_token(24)
    db.add(
        UserSession(
            token_hash=hash_token(raw),
            user_id=user.id,
            csrf_token=csrf,
            expires_at=datetime.now(UTC) + timedelta(hours=s.session_ttl_hours),
            ip_address=client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:300],
        )
    )
    user.last_login_at = datetime.now(UTC)
    audit(db, user.id, "auth.login", "user", user.id, ip=client_ip(request))
    db.commit()
    _set_session_cookies(response, raw, csrf)
    return SessionOut(user=user_out(db, user), csrf_token=csrf)


@router.post("/logout", response_model=Message)
def logout(request: Request, response: Response, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    sess = getattr(request.state, "session", None)
    if sess:
        sess.revoked = True
        db.commit()
    _clear_session_cookies(response)
    return Message(message="Signed out")


@router.get("/me", response_model=SessionOut)
def me(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return SessionOut(user=user_out(db, user), csrf_token=request.state.session.csrf_token)


@router.post("/verify-email", response_model=Message)
def verify_email(body: TokenIn, request: Request, db: Session = Depends(get_db)):
    _limit(db, request, "verify")
    tok = db.scalar(
        select(EmailVerificationToken)
        .where(EmailVerificationToken.token_hash == hash_token(body.token))
        .with_for_update(of=EmailVerificationToken)
    )
    if not tok or tok.used_at or tok.expires_at <= datetime.now(UTC):
        raise bad_request("This verification link is invalid or has expired. Request a new one.", "invalid_token")
    tok.used_at = datetime.now(UTC)
    user = db.get(User, tok.user_id)
    if user and not user.email_verified_at:
        user.email_verified_at = datetime.now(UTC)
        notify(db, user.id, "account.verified", "Email verified", "Welcome to DevOps Freshers!", "/dashboard")
        audit(db, user.id, "user.email_verified", "user", user.id, ip=client_ip(request))
    db.commit()
    return Message(message="Email verified. You can now sign in.")


@router.post("/resend-verification", response_model=Message)
def resend_verification(body: EmailIn, request: Request, db: Session = Depends(get_db)):
    _limit(db, request, "resend", body.email)
    user = find_user_by_email(db, body.email)
    if user and not user.email_verified_at:
        issue_email_verification(db, user)
        db.commit()
    return Message(message="If that account exists and is unverified, a new verification email has been sent.")


@router.post("/forgot-password", response_model=Message)
def forgot_password(body: EmailIn, request: Request, db: Session = Depends(get_db)):
    _limit(db, request, "forgot", body.email)
    user = find_user_by_email(db, body.email)
    if user and user.status == UserStatus.ACTIVE:
        issue_password_reset(db, user)
        audit(db, user.id, "auth.password_reset_requested", "user", user.id, ip=client_ip(request))
        db.commit()
    return Message(message="If an account exists for that email, a password reset link has been sent.")


@router.post("/reset-password", response_model=Message)
def reset_password(body: ResetPasswordIn, request: Request, db: Session = Depends(get_db)):
    _limit(db, request, "reset")
    tok = db.scalar(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == hash_token(body.token))
        .with_for_update(of=PasswordResetToken)
    )
    if not tok or tok.used_at or tok.expires_at <= datetime.now(UTC):
        raise bad_request("This reset link is invalid or has expired. Request a new one.", "invalid_token")
    tok.used_at = datetime.now(UTC)
    user = db.get(User, tok.user_id)
    user.password_hash = hash_password(body.password)
    # A user who can read the reset email has proven ownership of the address.
    user.email_verified_at = user.email_verified_at or datetime.now(UTC)
    db.execute(update(UserSession).where(UserSession.user_id == user.id).values(revoked=True))
    notify(
        db,
        user.id,
        "account.password_changed",
        "Password changed",
        "Your password was reset. All other sessions were signed out.",
    )
    audit(db, user.id, "auth.password_reset", "user", user.id, ip=client_ip(request))
    db.commit()
    return Message(message="Password updated. You can now sign in with your new password.")


@router.post("/change-password", response_model=Message)
def change_password(
    body: ChangePasswordIn, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    _limit(db, request, "change-password", str(user.id))
    if not verify_password(user.password_hash, body.current_password):
        raise bad_request("Current password is incorrect", "invalid_credentials")
    user.password_hash = hash_password(body.new_password)
    current = request.state.session.token_hash
    db.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id, UserSession.token_hash != current)
        .values(revoked=True)
    )
    audit(db, user.id, "auth.password_changed", "user", user.id, ip=client_ip(request))
    db.commit()
    return Message(message="Password changed. Other sessions were signed out.")
