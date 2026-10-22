from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_token, new_token
from app.models import EmailVerificationToken, PasswordResetToken, RegistrationApprovalRequest, User
from app.models.enums import ApprovalStatus
from app.schemas.auth import ProfileOut, UserOut
from app.services.notify import queue_email


def user_out(db: Session, user: User) -> UserOut:
    pending = db.scalar(
        select(RegistrationApprovalRequest.requested_role).where(
            RegistrationApprovalRequest.user_id == user.id,
            RegistrationApprovalRequest.status == ApprovalStatus.PENDING,
        )
    )
    return UserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        status=user.status,
        email_verified=user.email_verified_at is not None,
        created_at=user.created_at,
        profile=ProfileOut.model_validate(user.profile) if user.profile else None,
        pending_role_request=pending,
    )


def find_user_by_email(db: Session, email: str) -> User | None:
    from sqlalchemy import func

    return db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))


def issue_email_verification(db: Session, user: User) -> str:
    s = get_settings()
    token = new_token()
    db.add(
        EmailVerificationToken(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=datetime.now(UTC) + timedelta(hours=s.email_verification_ttl_hours),
        )
    )
    db.flush()
    link = f"{s.frontend_url}/verify-email?token={token}"
    queue_email(
        db,
        user.email,
        "Verify your DevOps Freshers email address",
        f"Hi {user.full_name},\n\nPlease confirm your email address by opening the link below "
        f"(valid for {s.email_verification_ttl_hours} hours):\n\n{link}\n\n"
        "If you did not create an account, you can ignore this email.",
        dedupe_key=f"verify:{hash_token(token)}",
    )
    return token


def issue_password_reset(db: Session, user: User) -> str:
    s = get_settings()
    token = new_token()
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=datetime.now(UTC) + timedelta(minutes=s.password_reset_ttl_minutes),
        )
    )
    db.flush()
    link = f"{s.frontend_url}/reset-password?token={token}"
    queue_email(
        db,
        user.email,
        "Reset your DevOps Freshers password",
        f"Hi {user.full_name},\n\nWe received a request to reset your password. Open the link below "
        f"within {s.password_reset_ttl_minutes} minutes to choose a new password:\n\n{link}\n\n"
        "If you did not request this, you can safely ignore this email.",
        dedupe_key=f"reset:{hash_token(token)}",
    )
    return token
