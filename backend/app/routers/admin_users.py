import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import client_ip, require_admin
from app.core.errors import bad_request, conflict, not_found
from app.models import AuditLog, RegistrationApprovalRequest, User, UserSession
from app.models.enums import ApprovalStatus, Role, UserStatus
from app.schemas.admin import AdminUserOut, AuditLogOut, RoleChangeIn, StatusChangeIn
from app.schemas.auth import ApprovalRequestOut, DecisionIn
from app.schemas.common import Message, Page
from app.services.audit import audit
from app.services.notify import notify, queue_email
from app.services.pagination import PageParams, page_params, paginate

router = APIRouter(prefix="/admin", tags=["admin"])


def _approval_out(r: RegistrationApprovalRequest) -> ApprovalRequestOut:
    return ApprovalRequestOut(
        id=r.id,
        requested_role=r.requested_role,
        status=r.status,
        motivation=r.motivation,
        expertise=r.expertise,
        created_at=r.created_at,
        decided_at=r.decided_at,
        decision_note=r.decision_note,
        applicant_id=r.user.id,
        applicant_name=r.user.full_name,
        applicant_email=r.user.email,
        applicant_email_verified=r.user.email_verified_at is not None,
    )


@router.get("/approval-requests", response_model=Page[ApprovalRequestOut])
def list_approval_requests(
    status: ApprovalStatus | None = ApprovalStatus.PENDING,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    stmt = select(RegistrationApprovalRequest).order_by(RegistrationApprovalRequest.created_at.desc())
    if status:
        stmt = stmt.where(RegistrationApprovalRequest.status == status)
    rows, total = paginate(db, stmt, params)
    return Page(items=[_approval_out(r) for r in rows], total=total, page=params.page, page_size=params.page_size)


def _decide(db: Session, request_id: uuid.UUID, approve: bool, note: str | None, admin: User, ip: str | None):
    req = db.scalar(
        select(RegistrationApprovalRequest)
        .where(RegistrationApprovalRequest.id == request_id)
        .with_for_update(of=RegistrationApprovalRequest)
    )
    if not req:
        raise not_found("Approval request")
    if req.status != ApprovalStatus.PENDING:
        raise conflict("This request has already been decided")
    applicant = req.user
    if applicant.id == admin.id:
        raise bad_request("You cannot decide on your own request")
    req.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
    req.decided_by_id = admin.id
    req.decided_at = datetime.now(UTC)
    req.decision_note = note
    role = req.requested_role.value.lower()
    if approve:
        previous = applicant.role
        applicant.role = req.requested_role
        # Force re-login so the new role takes effect with a fresh session.
        db.execute(update(UserSession).where(UserSession.user_id == applicant.id).values(revoked=True))
        notify(
            db,
            applicant.id,
            "approval.approved",
            f"Your {role} request was approved",
            "Sign in again to access your new dashboard.",
            "/dashboard",
        )
        body = (
            f"Hi {applicant.full_name},\n\nYour request for the {role} role on DevOps Freshers has been "
            "approved. Sign in to access your dashboard."
        )
        if not applicant.email_verified_at:
            body += "\n\nPlease verify your email address first using the link we sent when you registered."
        audit(
            db,
            admin.id,
            "approval.approved",
            "user",
            applicant.id,
            {"request_id": str(req.id), "from_role": previous.value, "to_role": req.requested_role.value},
            ip,
        )
    else:
        notify(db, applicant.id, "approval.rejected", f"Your {role} request was not approved", note)
        body = (
            f"Hi {applicant.full_name},\n\nYour request for the {role} role on DevOps Freshers was not "
            f"approved.\n\nReason: {note or 'Not specified'}\n\nYou can continue to use your student account."
        )
        audit(db, admin.id, "approval.rejected", "user", applicant.id, {"request_id": str(req.id), "note": note}, ip)
    queue_email(db, applicant.email, f"Your DevOps Freshers {role} request", body, f"role-decision:{req.id}")
    db.commit()
    return _approval_out(req)


@router.post("/approval-requests/{request_id}/approve", response_model=ApprovalRequestOut)
def approve_request(
    request_id: uuid.UUID,
    body: DecisionIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    return _decide(db, request_id, True, body.note, admin, client_ip(request))


@router.post("/approval-requests/{request_id}/reject", response_model=ApprovalRequestOut)
def reject_request(
    request_id: uuid.UUID,
    body: DecisionIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    return _decide(db, request_id, False, body.note, admin, client_ip(request))


def _admin_user_out(u: User) -> AdminUserOut:
    return AdminUserOut(
        id=u.id,
        email=u.email,
        full_name=u.full_name,
        role=u.role,
        status=u.status,
        email_verified=u.email_verified_at is not None,
        created_at=u.created_at,
        last_login_at=u.last_login_at,
    )


@router.get("/users", response_model=Page[AdminUserOut])
def list_users(
    q: str | None = Query(None, max_length=100),
    role: Role | None = None,
    status: UserStatus | None = None,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    stmt = select(User).order_by(User.created_at.desc())
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(User.email).like(like), func.lower(User.full_name).like(like)))
    if role:
        stmt = stmt.where(User.role == role)
    if status:
        stmt = stmt.where(User.status == status)
    rows, total = paginate(db, stmt, params)
    return Page(items=[_admin_user_out(u) for u in rows], total=total, page=params.page, page_size=params.page_size)


@router.get("/users/{user_id}", response_model=AdminUserOut)
def get_user(user_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    u = db.get(User, user_id)
    if not u:
        raise not_found("User")
    return _admin_user_out(u)


def _active_admin_count(db: Session) -> int:
    return (
        db.scalar(
            select(func.count()).select_from(User).where(User.role == Role.ADMIN, User.status == UserStatus.ACTIVE)
        )
        or 0
    )


@router.patch("/users/{user_id}/role", response_model=AdminUserOut)
def change_role(
    user_id: uuid.UUID,
    body: RoleChangeIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    u = db.scalar(select(User).where(User.id == user_id).with_for_update(of=User))
    if not u:
        raise not_found("User")
    if u.id == admin.id:
        raise bad_request("You cannot change your own role")
    if u.role == Role.ADMIN and body.role != Role.ADMIN and _active_admin_count(db) <= 1:
        raise bad_request("Cannot demote the last active Admin")
    previous = u.role
    u.role = body.role
    db.execute(update(UserSession).where(UserSession.user_id == u.id).values(revoked=True))
    notify(db, u.id, "account.role_changed", f"Your role is now {body.role.value.title()}", body.reason)
    audit(
        db,
        admin.id,
        "user.role_changed",
        "user",
        u.id,
        {"from": previous.value, "to": body.role.value, "reason": body.reason},
        client_ip(request),
    )
    db.commit()
    return _admin_user_out(u)


@router.patch("/users/{user_id}/status", response_model=AdminUserOut)
def change_status(
    user_id: uuid.UUID,
    body: StatusChangeIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    u = db.scalar(select(User).where(User.id == user_id).with_for_update(of=User))
    if not u:
        raise not_found("User")
    if u.id == admin.id:
        raise bad_request("You cannot change your own account status")
    if u.role == Role.ADMIN and body.status != UserStatus.ACTIVE and _active_admin_count(db) <= 1:
        raise bad_request("Cannot suspend the last active Admin")
    previous = u.status
    u.status = body.status
    if body.status == UserStatus.SUSPENDED:
        db.execute(update(UserSession).where(UserSession.user_id == u.id).values(revoked=True))
    queue_email(
        db,
        u.email,
        "Your DevOps Freshers account status changed",
        f"Hi {u.full_name},\n\nYour account status is now {body.status.value.lower()}."
        + (f"\n\nReason: {body.reason}" if body.reason else ""),
        f"status:{u.id}:{body.status.value}:{datetime.now(UTC).isoformat()}",
    )
    audit(
        db,
        admin.id,
        "user.status_changed",
        "user",
        u.id,
        {"from": previous.value, "to": body.status.value, "reason": body.reason},
        client_ip(request),
    )
    db.commit()
    return _admin_user_out(u)


@router.post("/users/{user_id}/send-password-reset", response_model=Message)
def admin_send_reset(
    user_id: uuid.UUID, request: Request, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    from app.services.users import issue_password_reset

    u = db.get(User, user_id)
    if not u:
        raise not_found("User")
    issue_password_reset(db, u)
    audit(db, admin.id, "user.password_reset_sent", "user", u.id, ip=client_ip(request))
    db.commit()
    return Message(message="Password reset email sent to the user")


@router.get("/audit-logs", response_model=Page[AuditLogOut])
def audit_logs(
    action: str | None = Query(None, max_length=80),
    entity_type: str | None = Query(None, max_length=60),
    entity_id: str | None = Query(None, max_length=64),
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    stmt = (
        select(AuditLog, User.full_name)
        .outerjoin(User, User.id == AuditLog.actor_id)
        .order_by(AuditLog.created_at.desc())
    )
    if action:
        stmt = stmt.where(AuditLog.action.like(f"{action}%"))
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    rows, total = paginate(db, stmt, params, scalars=False)
    items = [AuditLogOut.model_validate(log).model_copy(update={"actor_name": name}) for log, name in rows]
    return Page(items=items, total=total, page=params.page, page_size=params.page_size)
