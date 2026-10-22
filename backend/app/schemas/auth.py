import re
import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.enums import ApprovalStatus, Role, UserStatus
from app.schemas.common import ORMModel

PASSWORD_HELP = "Password must be 10-128 characters and include a letter and a number"  # noqa: S105


def _validate_password(v: str) -> str:
    if len(v) < 10 or len(v) > 128 or not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
        raise ValueError(PASSWORD_HELP)
    return v


class RegisterIn(BaseModel):
    email: EmailStr
    password: str
    full_name: str = Field(min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=32)
    requested_role: Role = Role.STUDENT
    motivation: str | None = Field(default=None, max_length=2000)
    expertise: str | None = Field(default=None, max_length=300)

    _pw = field_validator("password")(_validate_password)

    @field_validator("full_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class TokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class EmailIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str

    _pw = field_validator("password")(_validate_password)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str

    _pw = field_validator("new_password")(_validate_password)


class ProfileOut(ORMModel):
    phone: str | None = None
    headline: str | None = None
    bio: str | None = None
    city: str | None = None
    linkedin_url: str | None = None


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    status: UserStatus
    email_verified: bool
    created_at: datetime
    profile: ProfileOut | None = None
    pending_role_request: Role | None = None


class SessionOut(BaseModel):
    user: UserOut
    csrf_token: str


class ProfileUpdateIn(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=32)
    headline: str | None = Field(default=None, max_length=160)
    bio: str | None = Field(default=None, max_length=2000)
    city: str | None = Field(default=None, max_length=80)
    linkedin_url: str | None = Field(default=None, max_length=300, pattern=r"^(https://.*)?$")


class RoleRequestIn(BaseModel):
    requested_role: Role
    motivation: str | None = Field(default=None, max_length=2000)
    expertise: str | None = Field(default=None, max_length=300)


class ApprovalRequestOut(ORMModel):
    id: uuid.UUID
    requested_role: Role
    status: ApprovalStatus
    motivation: str | None
    expertise: str | None
    created_at: datetime
    decided_at: datetime | None
    decision_note: str | None
    applicant_id: uuid.UUID
    applicant_name: str
    applicant_email: str
    applicant_email_verified: bool


class DecisionIn(BaseModel):
    note: str | None = Field(default=None, max_length=2000)
