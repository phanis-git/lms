from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import client_ip, get_current_user
from app.core.errors import bad_request, conflict
from app.models import RegistrationApprovalRequest, User, UserProfile
from app.models.enums import ApprovalStatus, Role
from app.schemas.auth import ProfileUpdateIn, RoleRequestIn, UserOut
from app.schemas.common import Message
from app.services.approvals import create_role_request
from app.services.audit import audit
from app.services.users import user_out

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut)
def get_me(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return user_out(db, user)


@router.patch("/me", response_model=UserOut)
def update_me(body: ProfileUpdateIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    data = body.model_dump(exclude_unset=True)
    if "full_name" in data and data["full_name"]:
        user.full_name = data.pop("full_name").strip()
    else:
        data.pop("full_name", None)
    if not user.profile:
        user.profile = UserProfile()
    for k, v in data.items():
        setattr(user.profile, k, v or None)
    db.commit()
    db.refresh(user)
    return user_out(db, user)


@router.post("/me/role-requests", response_model=Message, status_code=201)
def request_role(
    body: RoleRequestIn, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if body.requested_role == Role.STUDENT:
        raise bad_request("Choose Instructor or Admin")
    if user.role == body.requested_role or user.role == Role.ADMIN:
        raise bad_request("You already have this role")
    existing = db.scalar(
        select(RegistrationApprovalRequest.id).where(
            RegistrationApprovalRequest.user_id == user.id,
            RegistrationApprovalRequest.status == ApprovalStatus.PENDING,
        )
    )
    if existing:
        raise conflict("You already have a pending role request")
    create_role_request(db, user, body.requested_role, body.motivation, body.expertise)
    audit(db, user.id, "user.role_requested", "user", user.id, {"role": body.requested_role.value}, client_ip(request))
    db.commit()
    return Message(message="Request submitted. An Admin will review it.")
