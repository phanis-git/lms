import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import client_ip, get_current_user, public_origin, require_admin
from app.core.errors import AppError, bad_request, conflict, not_found
from app.models import Course, Enrollment, MediaAsset, PaymentRecord, User
from app.models.enums import (
    EnrollmentSource,
    EnrollmentStatus,
    MediaStatus,
    PaymentMethod,
    PaymentStatus,
)
from app.schemas.commerce import (
    EnrollmentOut,
    EnrollmentStatusOut,
    GrantEnrollmentIn,
    ManualPaymentIn,
    PaymentConfigOut,
    PaymentDecisionIn,
    PaymentOut,
    RefundIn,
    RevokeEnrollmentIn,
)
from app.schemas.common import Page
from app.services import storage
from app.services.access import active_enrollment, get_course
from app.services.audit import audit
from app.services.notify import notify, notify_admins, queue_email
from app.services.pagination import PageParams, page_params, paginate
from app.services.payments import activate_enrollment, get_gateway, notify_payment_decision, record_event
from app.services.progress import course_progress_map
from app.services.ratelimit import enforce_rate_limit
from app.services.settings_store import get_setting

router = APIRouter(tags=["payments"])


def payment_out(p: PaymentRecord) -> PaymentOut:
    return PaymentOut(
        id=p.id,
        course_id=p.course_id,
        course_title=p.course.title,
        student_id=p.student_id,
        student_name=p.student.full_name,
        student_email=p.student.email,
        method=p.method,
        status=p.status,
        amount_inr=p.amount_paise / 100,
        expected_amount_inr=p.expected_amount_paise / 100,
        amount_matches=p.amount_paise == p.expected_amount_paise,
        transaction_reference=p.transaction_reference,
        payer_upi_id=p.payer_upi_id,
        paid_at=p.paid_at,
        has_screenshot=p.screenshot_asset_id is not None,
        student_note=p.student_note,
        rejection_reason=p.rejection_reason,
        verified_by_name=p.verified_by.full_name if p.verified_by else None,
        verified_at=p.verified_at,
        refunded_amount_inr=p.refunded_amount_paise / 100,
        created_at=p.created_at,
    )


@router.get("/payments/config", response_model=PaymentConfigOut)
def payment_config(request: Request, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    cfg = get_setting(db, "payment")
    qr_url = None
    if cfg.get("qr_asset_id"):
        a = db.get(MediaAsset, uuid.UUID(cfg["qr_asset_id"]))
        if a and a.status == MediaStatus.READY:
            qr_url = storage.signed_get_url(a.storage_key, public_origin(request), ttl=900)
    return PaymentConfigOut(
        upi_id=cfg.get("upi_id"),
        payee_name=cfg.get("payee_name"),
        instructions=cfg.get("instructions"),
        qr_asset_id=cfg.get("qr_asset_id"),
        qr_url=qr_url,
        gateway_enabled=get_gateway() is not None,
    )


@router.post("/payments/submit-manual-payment", response_model=PaymentOut, status_code=201)
def submit_manual_payment(
    body: ManualPaymentIn, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    enforce_rate_limit(db, f"payment-submit:{user.id}", 5, 3600)
    course = get_course(db, body.course_id)
    if not course.is_purchasable:
        raise AppError(409, "not_purchasable", "This course is not available for purchase")
    if course.owner_id == user.id or user.is_admin:
        raise bad_request("You already have access to manage this course")
    if active_enrollment(db, user, course.id):
        raise conflict("You are already enrolled in this course", "already_enrolled")
    if db.scalar(
        select(PaymentRecord.id).where(
            PaymentRecord.student_id == user.id,
            PaymentRecord.course_id == course.id,
            PaymentRecord.status == PaymentStatus.PENDING_VERIFICATION,
        )
    ):
        raise conflict("You already have a payment awaiting verification for this course", "payment_pending")
    amount = round(body.amount_inr * 100)
    if amount != course.price_paise:
        raise bad_request(
            f"The amount must match the course price of ₹{course.price_paise / 100:,.2f}", "amount_mismatch"
        )
    now = datetime.now(UTC)
    paid_at = body.paid_at if body.paid_at.tzinfo else body.paid_at.replace(tzinfo=UTC)
    if paid_at > now + timedelta(minutes=10) or paid_at < now - timedelta(days=30):
        raise bad_request("Payment date must be within the last 30 days")
    if body.screenshot_asset_id:
        a = db.get(MediaAsset, body.screenshot_asset_id)
        if not a or a.owner_id != user.id or a.purpose != "payment_screenshot" or a.status != MediaStatus.READY:
            raise bad_request("Invalid screenshot")
    p = PaymentRecord(
        student_id=user.id,
        course_id=course.id,
        method=PaymentMethod.MANUAL_UPI,
        status=PaymentStatus.PENDING_VERIFICATION,
        amount_paise=amount,
        expected_amount_paise=course.price_paise,
        transaction_reference=body.transaction_reference,
        payer_upi_id=body.payer_upi_id,
        paid_at=paid_at,
        screenshot_asset_id=body.screenshot_asset_id,
        student_note=body.note,
    )
    db.add(p)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise conflict("This UPI transaction reference has already been submitted", "duplicate_reference")
    record_event(db, p.id, user.id, "submitted")
    notify_admins(
        db,
        "payment.submitted",
        "Payment awaiting verification",
        f"{user.full_name} submitted ₹{amount / 100:,.2f} for {course.title}.",
        "/admin/payments",
    )
    notify(
        db,
        user.id,
        "payment.submitted",
        "Payment submitted",
        f"We will verify your payment for {course.title} and notify you.",
        "/student/purchases",
    )
    audit(
        db,
        user.id,
        "payment.submitted",
        "payment",
        p.id,
        {"course_id": str(course.id), "amount_paise": amount},
        client_ip(request),
    )
    db.commit()
    db.refresh(p)
    return payment_out(p)


@router.get("/payments/my-payments", response_model=list[PaymentOut])
def my_payments(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (
        db.scalars(
            select(PaymentRecord).where(PaymentRecord.student_id == user.id).order_by(PaymentRecord.created_at.desc())
        )
        .unique()
        .all()
    )
    return [payment_out(p) for p in rows]


@router.get("/admin/payments", response_model=Page[PaymentOut])
def admin_payments(
    status: PaymentStatus | None = None,
    q: str | None = Query(None, max_length=100),
    course_id: uuid.UUID | None = None,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    stmt = select(PaymentRecord).order_by(PaymentRecord.created_at.desc())
    if status:
        stmt = stmt.where(PaymentRecord.status == status)
    if course_id:
        stmt = stmt.where(PaymentRecord.course_id == course_id)
    if q:
        ref = "".join(q.split()).upper()
        stmt = stmt.join(User, User.id == PaymentRecord.student_id).where(
            (PaymentRecord.transaction_reference == ref) | User.email.ilike(f"%{q.strip()}%")
        )
    rows, total = paginate(db, stmt, params)
    return Page(items=[payment_out(p) for p in rows], total=total, page=params.page, page_size=params.page_size)


@router.get("/admin/payments/pending", response_model=Page[PaymentOut])
def pending_payments(
    params: PageParams = Depends(page_params), db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    return admin_payments(PaymentStatus.PENDING_VERIFICATION, None, None, params, db, admin)


@router.get("/admin/payments/{payment_id}/screenshot")
def payment_screenshot(
    payment_id: uuid.UUID, request: Request, db: Session = Depends(get_db), _: User = Depends(require_admin)
):
    p = db.get(PaymentRecord, payment_id)
    if not p or not p.screenshot_asset_id:
        raise not_found("Screenshot")
    a = db.get(MediaAsset, p.screenshot_asset_id)
    return {"url": storage.signed_get_url(a.storage_key, public_origin(request), ttl=300), "expires_in": 300}


def _locked_payment(db: Session, payment_id: uuid.UUID) -> PaymentRecord:
    p = db.scalar(select(PaymentRecord).where(PaymentRecord.id == payment_id).with_for_update(of=PaymentRecord))
    if not p:
        raise not_found("Payment")
    return p


@router.post("/admin/payments/{payment_id}/approve", response_model=PaymentOut)
def approve_payment(
    payment_id: uuid.UUID,
    body: PaymentDecisionIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    p = _locked_payment(db, payment_id)
    if p.status != PaymentStatus.PENDING_VERIFICATION:
        raise conflict(f"Payment is already {p.status.value.replace('_', ' ').lower()}", "already_processed")
    p.status = PaymentStatus.APPROVED
    p.verified_by_id = admin.id
    p.verified_at = datetime.now(UTC)
    e = activate_enrollment(db, p.student_id, p.course_id, EnrollmentSource.PAYMENT, p.id)
    record_event(db, p.id, admin.id, "approved", body.reason)
    notify_payment_decision(db, p, True, p.student)
    audit(
        db,
        admin.id,
        "payment.approved",
        "payment",
        p.id,
        {"enrollment_id": str(e.id), "amount_paise": p.amount_paise},
        client_ip(request),
    )
    db.commit()
    db.refresh(p)
    return payment_out(p)


@router.post("/admin/payments/{payment_id}/reject", response_model=PaymentOut)
def reject_payment(
    payment_id: uuid.UUID,
    body: PaymentDecisionIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    if not body.reason:
        raise bad_request("Please give the student a reason", "reason_required")
    p = _locked_payment(db, payment_id)
    if p.status != PaymentStatus.PENDING_VERIFICATION:
        raise conflict(f"Payment is already {p.status.value.replace('_', ' ').lower()}", "already_processed")
    p.status = PaymentStatus.REJECTED
    p.rejection_reason = body.reason
    p.verified_by_id = admin.id
    p.verified_at = datetime.now(UTC)
    record_event(db, p.id, admin.id, "rejected", body.reason)
    notify_payment_decision(db, p, False, p.student)
    audit(db, admin.id, "payment.rejected", "payment", p.id, {"reason": body.reason}, client_ip(request))
    db.commit()
    db.refresh(p)
    return payment_out(p)


@router.post("/admin/payments/{payment_id}/refund", response_model=PaymentOut)
def refund_payment(
    payment_id: uuid.UUID,
    body: RefundIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Records a refund the Admin has made outside the LMS (manual UPI). Gateways would call the provider."""
    p = _locked_payment(db, payment_id)
    if p.status != PaymentStatus.APPROVED:
        raise conflict("Only verified payments can be refunded")
    amount = round(body.amount_inr * 100) if body.amount_inr else p.amount_paise
    if amount > p.amount_paise - p.refunded_amount_paise:
        raise bad_request("Refund exceeds the remaining paid amount")
    p.refunded_amount_paise += amount
    p.refunded_at = datetime.now(UTC)
    if p.refunded_amount_paise >= p.amount_paise:
        p.status = PaymentStatus.REFUNDED
    if body.revoke_access:
        e = db.scalar(
            select(Enrollment).where(Enrollment.student_id == p.student_id, Enrollment.course_id == p.course_id)
        )
        if e and e.status == EnrollmentStatus.ACTIVE:
            e.status = EnrollmentStatus.REVOKED
            e.revoked_at = datetime.now(UTC)
            e.revoke_reason = f"Refund: {body.reason}"
    record_event(db, p.id, admin.id, "refunded", f"{amount / 100:.2f}: {body.reason}")
    notify(
        db,
        p.student_id,
        "payment.refunded",
        "Refund recorded",
        f"A refund of ₹{amount / 100:,.2f} for {p.course.title} was recorded.",
    )
    queue_email(
        db,
        p.student.email,
        f"Refund recorded: {p.course.title}",
        f'Hi {p.student.full_name},\n\nA refund of ₹{amount / 100:,.2f} for "{p.course.title}" has been '
        f"recorded.\n\nReason: {body.reason}",
        f"refund:{p.id}:{p.refunded_amount_paise}",
    )
    audit(
        db,
        admin.id,
        "payment.refunded",
        "payment",
        p.id,
        {"amount_paise": amount, "revoke_access": body.revoke_access},
        client_ip(request),
    )
    db.commit()
    db.refresh(p)
    return payment_out(p)


# ---------- Enrollments ----------
def enrollment_out(e: Enrollment, progress: int = 0) -> EnrollmentOut:
    return EnrollmentOut(
        id=e.id,
        course_id=e.course_id,
        course_title=e.course.title,
        course_slug=e.course.slug,
        student_id=e.student_id,
        student_name=e.student.full_name,
        student_email=e.student.email,
        status=e.status,
        source=e.source,
        activated_at=e.activated_at,
        completed_at=e.completed_at,
        last_accessed_at=e.last_accessed_at,
        progress_percent=progress,
    )


@router.get("/enrollments/my-courses", response_model=list[EnrollmentOut])
def my_courses(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (
        db.scalars(
            select(Enrollment)
            .where(Enrollment.student_id == user.id, Enrollment.status == EnrollmentStatus.ACTIVE)
            .order_by(Enrollment.last_accessed_at.desc().nullslast(), Enrollment.activated_at.desc())
        )
        .unique()
        .all()
    )
    prog = course_progress_map(db, user.id, [e.course_id for e in rows])
    return [enrollment_out(e, prog.get(e.course_id, 0)) for e in rows]


@router.get("/courses/{course_id}/enrollment-status", response_model=EnrollmentStatusOut)
def enrollment_status(course_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    e = db.scalar(select(Enrollment).where(Enrollment.student_id == user.id, Enrollment.course_id == course_id))
    payments = (
        db.scalars(
            select(PaymentRecord)
            .where(PaymentRecord.student_id == user.id, PaymentRecord.course_id == course_id)
            .order_by(PaymentRecord.created_at.desc())
        )
        .unique()
        .all()
    )
    pending = any(p.status == PaymentStatus.PENDING_VERIFICATION for p in payments)
    last_rej = next((p.rejection_reason for p in payments if p.status == PaymentStatus.REJECTED), None)
    return EnrollmentStatusOut(
        enrolled=bool(e and e.status == EnrollmentStatus.ACTIVE),
        status=e.status if e else None,
        pending_payment=pending,
        last_rejection_reason=None if pending else last_rej,
    )


@router.get("/admin/enrollments", response_model=Page[EnrollmentOut])
def admin_enrollments(
    course_id: uuid.UUID | None = None,
    student_id: uuid.UUID | None = None,
    status: EnrollmentStatus | None = None,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    stmt = select(Enrollment).order_by(Enrollment.created_at.desc())
    if course_id:
        stmt = stmt.where(Enrollment.course_id == course_id)
    if student_id:
        stmt = stmt.where(Enrollment.student_id == student_id)
    if status:
        stmt = stmt.where(Enrollment.status == status)
    rows, total = paginate(db, stmt, params)
    return Page(items=[enrollment_out(e) for e in rows], total=total, page=params.page, page_size=params.page_size)


@router.post("/admin/enrollments/grant", response_model=EnrollmentOut, status_code=201)
def grant_enrollment(
    body: GrantEnrollmentIn, request: Request, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    student = db.get(User, body.student_id)
    course = db.get(Course, body.course_id)
    if not student or not course or course.deleted_at:
        raise not_found("Student or course")
    e = activate_enrollment(db, student.id, course.id, EnrollmentSource.ADMIN_GRANT, None)
    notify(
        db, student.id, "enrollment.activated", f"You now have access to {course.title}", None, f"/learn/{course.id}"
    )
    audit(
        db,
        admin.id,
        "enrollment.granted",
        "enrollment",
        e.id,
        {"student_id": str(student.id), "course_id": str(course.id), "reason": body.reason},
        client_ip(request),
    )
    db.commit()
    return enrollment_out(e)


@router.post("/admin/enrollments/{enrollment_id}/revoke", response_model=EnrollmentOut)
def revoke_enrollment(
    enrollment_id: uuid.UUID,
    body: RevokeEnrollmentIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    e = db.get(Enrollment, enrollment_id)
    if not e:
        raise not_found("Enrollment")
    e.status = EnrollmentStatus.REVOKED
    e.revoked_at = datetime.now(UTC)
    e.revoke_reason = body.reason
    audit(db, admin.id, "enrollment.revoked", "enrollment", e.id, {"reason": body.reason}, client_ip(request))
    db.commit()
    return enrollment_out(e)
