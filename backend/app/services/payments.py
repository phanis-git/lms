"""Payments: manual UPI verification now, pluggable gateway (e.g. Razorpay) later."""

import uuid
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Enrollment, PaymentRecord, PaymentVerificationEvent, User
from app.models.enums import EnrollmentSource, EnrollmentStatus
from app.services.notify import notify, queue_email


class PaymentGateway(Protocol):
    """Interface a real provider must implement. No provider is configured by default."""

    name: str

    def create_order(self, payment: PaymentRecord) -> dict: ...

    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool: ...

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool: ...

    def refund(self, payment: PaymentRecord, amount_paise: int) -> dict: ...


def get_gateway() -> PaymentGateway | None:
    return None


def record_event(
    db: Session, payment_id: uuid.UUID, actor_id: uuid.UUID | None, action: str, note: str | None = None
) -> None:
    db.add(PaymentVerificationEvent(payment_id=payment_id, actor_id=actor_id, action=action, note=note))


def activate_enrollment(
    db: Session, student_id: uuid.UUID, course_id: uuid.UUID, source: EnrollmentSource, payment_id: uuid.UUID | None
) -> Enrollment:
    """Idempotent: ON CONFLICT on (student, course) guarantees a single enrollment row."""
    now = datetime.now(UTC)
    db.execute(
        insert(Enrollment)
        .values(
            id=uuid.uuid4(),
            student_id=student_id,
            course_id=course_id,
            status=EnrollmentStatus.ACTIVE,
            source=source,
            payment_id=payment_id,
            activated_at=now,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_enrollment_student_course")
    )
    e = db.scalar(
        select(Enrollment)
        .where(Enrollment.student_id == student_id, Enrollment.course_id == course_id)
        .with_for_update(of=Enrollment)
    )
    if e.status != EnrollmentStatus.ACTIVE:
        e.status = EnrollmentStatus.ACTIVE
        e.source = source
        e.payment_id = payment_id or e.payment_id
        e.activated_at = now
        e.revoked_at = None
        e.revoke_reason = None
    return e


def notify_payment_decision(db: Session, p: PaymentRecord, approved: bool, student: User) -> None:
    title = p.course.title
    if approved:
        notify(
            db,
            student.id,
            "payment.approved",
            "Payment verified - course unlocked",
            f"You now have access to {title}.",
            f"/learn/{p.course_id}",
        )
        body = (
            f'Hi {student.full_name},\n\nYour payment (ref {p.transaction_reference}) for "{title}" has been '
            "verified. The course is now available in My Courses. Happy learning!"
        )
        subject = f"Access granted: {title}"
    else:
        notify(
            db,
            student.id,
            "payment.rejected",
            "Payment could not be verified",
            p.rejection_reason,
            f"/courses/{p.course.slug}",
        )
        body = (
            f"Hi {student.full_name},\n\nWe could not verify your payment (ref {p.transaction_reference}) "
            f'for "{title}".\n\nReason: {p.rejection_reason}\n\nIf you believe this is a mistake, reply '
            "to support with your UPI transaction details or submit the correct reference again."
        )
        subject = f"Payment not verified: {title}"
    queue_email(db, student.email, subject, body, f"payment-decision:{p.id}:{approved}")
