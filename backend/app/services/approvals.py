from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import RegistrationApprovalRequest, User
from app.models.enums import Role
from app.services.notify import notify_admins, queue_email


def create_role_request(
    db: Session, user: User, role: Role, motivation: str | None, expertise: str | None
) -> RegistrationApprovalRequest:
    s = get_settings()
    req = RegistrationApprovalRequest(user_id=user.id, requested_role=role, motivation=motivation, expertise=expertise)
    db.add(req)
    db.flush()
    link = f"{s.frontend_url}/admin/approvals?request={req.id}"
    queue_email(
        db,
        s.approval_notification_email,
        f"[DevOps Freshers] New {role.value.title()} account request: {user.full_name}",
        f"A new elevated-role request is waiting for review.\n\n"
        f"Requested role: {role.value}\nName: {user.full_name}\nEmail: {user.email}\n"
        f"Expertise: {expertise or '-'}\n\nMotivation:\n{motivation or '-'}\n\n"
        f"Review it in the Admin dashboard (sign-in required):\n{link}",
        dedupe_key=f"role-request:{req.id}",
    )
    notify_admins(
        db,
        "approval.requested",
        f"New {role.value.lower()} request",
        f"{user.full_name} ({user.email}) requested the {role.value.lower()} role.",
        "/admin/approvals",
    )
    return req
