import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.enums import EnrollmentSource, EnrollmentStatus, PaymentMethod, PaymentStatus


class ManualPaymentIn(BaseModel):
    course_id: uuid.UUID
    transaction_reference: str = Field(min_length=6, max_length=64)
    amount_inr: float = Field(gt=0, le=500000)
    paid_at: datetime
    payer_upi_id: str | None = Field(default=None, max_length=120)
    screenshot_asset_id: uuid.UUID | None = None
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("transaction_reference")
    @classmethod
    def _norm(cls, v: str) -> str:
        v = "".join(v.split()).upper()
        if not v.isalnum():
            raise ValueError("Transaction reference must contain only letters and numbers")
        return v


class PaymentOut(BaseModel):
    id: uuid.UUID
    course_id: uuid.UUID
    course_title: str
    student_id: uuid.UUID
    student_name: str
    student_email: str
    method: PaymentMethod
    status: PaymentStatus
    amount_inr: float
    expected_amount_inr: float
    amount_matches: bool
    transaction_reference: str | None
    payer_upi_id: str | None
    paid_at: datetime | None
    has_screenshot: bool
    student_note: str | None
    rejection_reason: str | None
    verified_by_name: str | None
    verified_at: datetime | None
    refunded_amount_inr: float
    created_at: datetime


class PaymentDecisionIn(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class RefundIn(BaseModel):
    amount_inr: float | None = Field(default=None, gt=0)
    reason: str = Field(min_length=3, max_length=1000)
    revoke_access: bool = True


class EnrollmentOut(BaseModel):
    id: uuid.UUID
    course_id: uuid.UUID
    course_title: str
    course_slug: str
    student_id: uuid.UUID
    student_name: str
    student_email: str
    status: EnrollmentStatus
    source: EnrollmentSource
    activated_at: datetime | None
    completed_at: datetime | None
    last_accessed_at: datetime | None
    progress_percent: int = 0


class EnrollmentStatusOut(BaseModel):
    enrolled: bool
    status: EnrollmentStatus | None
    pending_payment: bool
    last_rejection_reason: str | None = None


class GrantEnrollmentIn(BaseModel):
    student_id: uuid.UUID
    course_id: uuid.UUID
    reason: str = Field(min_length=3, max_length=500)


class RevokeEnrollmentIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class PaymentConfigOut(BaseModel):
    upi_id: str | None
    payee_name: str | None
    instructions: str | None
    qr_asset_id: uuid.UUID | None
    qr_url: str | None
    gateway_enabled: bool = False
