import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import Role, UserStatus
from app.schemas.common import ORMModel


class AdminUserOut(ORMModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    status: UserStatus
    email_verified: bool
    created_at: datetime
    last_login_at: datetime | None


class RoleChangeIn(BaseModel):
    role: Role
    reason: str | None = Field(default=None, max_length=500)


class StatusChangeIn(BaseModel):
    status: UserStatus
    reason: str | None = Field(default=None, max_length=500)


class AuditLogOut(ORMModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    actor_name: str | None = None
    action: str
    entity_type: str
    entity_id: str | None
    details: dict
    ip_address: str | None
    created_at: datetime
