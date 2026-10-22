import csv
import io
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import client_ip, get_current_user, require_admin, require_author
from app.core.errors import bad_request, not_found
from app.models import (
    Course,
    Enrollment,
    LessonProgress,
    MediaAsset,
    PaymentRecord,
    RegistrationApprovalRequest,
    User,
)
from app.models.enums import (
    ApprovalStatus,
    CourseStatus,
    EnrollmentStatus,
    MediaStatus,
    PaymentStatus,
    Role,
    UserStatus,
)
from app.schemas.commerce import PaymentConfigOut
from app.services.audit import audit
from app.services.progress import course_progress_detail
from app.services.settings_store import get_setting, put_setting

router = APIRouter(tags=["dashboards"])


def _count(db: Session, stmt) -> int:
    return int(db.scalar(stmt) or 0)


@router.get("/dashboard/admin")
def admin_dashboard(db: Session = Depends(get_db), _: User = Depends(require_admin)) -> dict:
    since = datetime.now(UTC) - timedelta(days=30)
    c = select(func.count())
    revenue = db.scalar(
        select(func.coalesce(func.sum(PaymentRecord.amount_paise - PaymentRecord.refunded_amount_paise), 0)).where(
            PaymentRecord.status.in_([PaymentStatus.APPROVED, PaymentStatus.REFUNDED])
        )
    )
    revenue_30 = db.scalar(
        select(func.coalesce(func.sum(PaymentRecord.amount_paise - PaymentRecord.refunded_amount_paise), 0)).where(
            PaymentRecord.status.in_([PaymentStatus.APPROVED, PaymentStatus.REFUNDED]),
            PaymentRecord.verified_at >= since,
        )
    )
    top = db.execute(
        select(Course.id, Course.title, func.count(Enrollment.id))
        .join(Enrollment, Enrollment.course_id == Course.id)
        .where(Enrollment.status == EnrollmentStatus.ACTIVE)
        .group_by(Course.id, Course.title)
        .order_by(func.count(Enrollment.id).desc())
        .limit(5)
    ).all()
    return {
        "students": _count(db, c.select_from(User).where(User.role == Role.STUDENT)),
        "instructors": _count(
            db, c.select_from(User).where(User.role == Role.INSTRUCTOR, User.status == UserStatus.ACTIVE)
        ),
        "published_courses": _count(
            db, c.select_from(Course).where(Course.status == CourseStatus.PUBLISHED, Course.deleted_at.is_(None))
        ),
        "courses_in_review": _count(
            db,
            c.select_from(Course).where(
                Course.status == CourseStatus.SUBMITTED_FOR_REVIEW, Course.deleted_at.is_(None)
            ),
        ),
        "pending_payments": _count(
            db, c.select_from(PaymentRecord).where(PaymentRecord.status == PaymentStatus.PENDING_VERIFICATION)
        ),
        "pending_approvals": _count(
            db,
            c.select_from(RegistrationApprovalRequest).where(
                RegistrationApprovalRequest.status == ApprovalStatus.PENDING
            ),
        ),
        "active_enrollments": _count(db, c.select_from(Enrollment).where(Enrollment.status == EnrollmentStatus.ACTIVE)),
        "new_enrollments_30d": _count(db, c.select_from(Enrollment).where(Enrollment.activated_at >= since)),
        "revenue_inr": int(revenue or 0) / 100,
        "revenue_30d_inr": int(revenue_30 or 0) / 100,
        "top_courses": [{"course_id": cid, "title": t, "enrollments": n} for cid, t, n in top],
    }


@router.get("/dashboard/instructor")
def instructor_dashboard(db: Session = Depends(get_db), user: User = Depends(require_author)) -> dict:
    stmt = select(Course).where(Course.deleted_at.is_(None)).order_by(Course.updated_at.desc())
    if not user.is_admin:
        stmt = stmt.where(Course.owner_id == user.id)
    courses = db.scalars(stmt).all()
    ids = [c.id for c in courses]
    enroll = (
        dict(
            db.execute(
                select(Enrollment.course_id, func.count())
                .where(Enrollment.course_id.in_(ids), Enrollment.status == EnrollmentStatus.ACTIVE)
                .group_by(Enrollment.course_id)
            ).all()
        )
        if ids
        else {}
    )
    completed = (
        dict(
            db.execute(
                select(Enrollment.course_id, func.count())
                .where(Enrollment.course_id.in_(ids), Enrollment.completed_at.isnot(None))
                .group_by(Enrollment.course_id)
            ).all()
        )
        if ids
        else {}
    )
    by_status: dict[str, int] = {}
    for c in courses:
        by_status[c.status.value] = by_status.get(c.status.value, 0) + 1
    return {
        "total_courses": len(courses),
        "by_status": by_status,
        "total_students": sum(enroll.values()),
        "courses": [
            {
                "id": c.id,
                "title": c.title,
                "status": c.status,
                "students": enroll.get(c.id, 0),
                "completions": completed.get(c.id, 0),
                "updated_at": c.updated_at,
            }
            for c in courses[:20]
        ],
    }


@router.get("/dashboard/student")
def student_dashboard(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    ens = (
        db.scalars(
            select(Enrollment)
            .where(Enrollment.student_id == user.id, Enrollment.status == EnrollmentStatus.ACTIVE)
            .order_by(Enrollment.last_accessed_at.desc().nullslast())
        )
        .unique()
        .all()
    )
    prog = course_progress_detail(db, user.id, [e.course_id for e in ens])
    pending = (
        db.scalars(
            select(PaymentRecord).where(
                PaymentRecord.student_id == user.id, PaymentRecord.status == PaymentStatus.PENDING_VERIFICATION
            )
        )
        .unique()
        .all()
    )
    last = db.scalar(
        select(LessonProgress)
        .where(LessonProgress.student_id == user.id)
        .order_by(LessonProgress.last_activity_at.desc())
        .limit(1)
    )
    return {
        "enrolled": len(ens),
        "completed": sum(1 for e in ens if e.completed_at),
        "in_progress": sum(1 for e in ens if not e.completed_at and prog.get(e.course_id, {}).get("percent", 0) > 0),
        "pending_payments": [
            {"id": p.id, "course_title": p.course.title, "amount_inr": p.amount_paise / 100, "created_at": p.created_at}
            for p in pending
        ],
        "continue_learning": {"course_id": last.course_id, "lesson_id": last.lesson_id} if last else None,
        "courses": [
            {
                "course_id": e.course_id,
                "title": e.course.title,
                "slug": e.course.slug,
                "progress_percent": prog.get(e.course_id, {}).get("percent", 0),
                "completed": bool(e.completed_at),
                "last_accessed_at": e.last_accessed_at,
            }
            for e in ens
        ],
    }


@router.get("/courses/{course_id}/students")
def course_students(course_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_author)) -> list:
    course = db.get(Course, course_id)
    if not course or (course.owner_id != user.id and not user.is_admin):
        raise not_found("Course")
    ens = (
        db.scalars(select(Enrollment).where(Enrollment.course_id == course.id).order_by(Enrollment.activated_at.desc()))
        .unique()
        .all()
    )
    out = []
    for e in ens:
        d = course_progress_detail(db, e.student_id, [course.id])[course.id]
        out.append(
            {
                "student_id": e.student_id,
                "name": e.student.full_name,
                "email": e.student.email,
                "status": e.status,
                "activated_at": e.activated_at,
                "last_accessed_at": e.last_accessed_at,
                "progress_percent": d["percent"],
                "completed_at": e.completed_at,
            }
        )
    return out


# ---------- Reports ----------
def _csv(name: str, header: list[str], rows) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        # Neutralise spreadsheet formula injection.
        w.writerow(["'" + str(v) if isinstance(v, str) and v[:1] in "=+-@" else v for v in r])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
    )


@router.get("/admin/reports/{kind}.csv")
def report(kind: str, request: Request, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    audit(db, admin.id, "report.exported", "report", None, {"kind": kind}, client_ip(request))
    db.commit()
    if kind == "payments":
        rows = db.scalars(select(PaymentRecord).order_by(PaymentRecord.created_at.desc())).unique().all()
        return _csv(
            "payments",
            [
                "submitted_at",
                "student",
                "email",
                "course",
                "amount_inr",
                "reference",
                "status",
                "verified_at",
                "refunded_inr",
            ],
            [
                (
                    p.created_at.isoformat(),
                    p.student.full_name,
                    p.student.email,
                    p.course.title,
                    p.amount_paise / 100,
                    p.transaction_reference,
                    p.status.value,
                    p.verified_at.isoformat() if p.verified_at else "",
                    p.refunded_amount_paise / 100,
                )
                for p in rows
            ],
        )
    if kind in ("enrollments", "progress"):
        ens = db.scalars(select(Enrollment).order_by(Enrollment.created_at.desc())).unique().all()
        by_student: dict = {}
        for e in ens:
            by_student.setdefault(e.student_id, []).append(e.course_id)
        prog = {sid: course_progress_detail(db, sid, cids) for sid, cids in by_student.items()}
        return _csv(
            kind,
            [
                "student",
                "email",
                "course",
                "status",
                "source",
                "activated_at",
                "progress_percent",
                "completed_at",
                "last_accessed_at",
            ],
            [
                (
                    e.student.full_name,
                    e.student.email,
                    e.course.title,
                    e.status.value,
                    e.source.value,
                    e.activated_at.isoformat() if e.activated_at else "",
                    prog[e.student_id][e.course_id]["percent"],
                    e.completed_at.isoformat() if e.completed_at else "",
                    e.last_accessed_at.isoformat() if e.last_accessed_at else "",
                )
                for e in ens
            ],
        )
    raise not_found("Report")


# ---------- Platform settings ----------
class PaymentSettingsIn(BaseModel):
    upi_id: str | None = Field(default=None, max_length=120, pattern=r"^[\w.\-]{2,}@[\w.\-]{2,}$")
    payee_name: str | None = Field(default=None, max_length=120)
    instructions: str | None = Field(default=None, max_length=2000)
    qr_asset_id: uuid.UUID | None = None


class SupportSettingsIn(BaseModel):
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    hours: str | None = Field(default=None, max_length=200)
    refund_policy: str | None = Field(default=None, max_length=5000)


@router.get("/settings/public")
def public_settings(db: Session = Depends(get_db)) -> dict:
    s = get_setting(db, "support")
    return {
        "support": {k: s.get(k) for k in ("email", "phone", "hours")},
        "refund_policy": get_setting(db, "policies").get("refund_policy"),
    }


@router.get("/admin/settings/payment", response_model=PaymentConfigOut)
def get_payment_settings(request: Request, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from app.routers.payments import payment_config

    return payment_config(request, db, admin)


@router.put("/admin/settings/payment", response_model=PaymentConfigOut)
def put_payment_settings(
    body: PaymentSettingsIn, request: Request, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    data = body.model_dump(exclude_unset=True)
    if data.get("qr_asset_id"):
        a = db.get(MediaAsset, data["qr_asset_id"])
        if not a or a.purpose != "upi_qr" or a.status != MediaStatus.READY:
            raise bad_request("Upload the QR image first")
        data["qr_asset_id"] = str(data["qr_asset_id"])
    put_setting(db, "payment", data, admin.id)
    audit(
        db,
        admin.id,
        "settings.payment_updated",
        "settings",
        None,
        {k: v for k, v in data.items() if k != "instructions"},
        client_ip(request),
    )
    db.commit()
    return get_payment_settings(request, db, admin)


@router.put("/admin/settings/support")
def put_support_settings(
    body: SupportSettingsIn, request: Request, db: Session = Depends(get_db), admin: User = Depends(require_admin)
) -> dict:
    data = body.model_dump(exclude_unset=True)
    policy = data.pop("refund_policy", None)
    put_setting(db, "support", data, admin.id)
    if policy is not None:
        put_setting(db, "policies", {"refund_policy": policy}, admin.id)
    audit(db, admin.id, "settings.support_updated", "settings", None, None, client_ip(request))
    db.commit()
    return public_settings(db)
