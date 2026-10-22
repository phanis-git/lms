from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import Course, CoursePublicationReview, Lesson, User
from app.models.enums import CourseStatus
from app.services.audit import audit
from app.services.notify import notify, notify_admins, queue_email

S = CourseStatus

# action -> (allowed source states, target state, admin only)
TRANSITIONS: dict[str, tuple[set[CourseStatus], CourseStatus, bool]] = {
    "submit": ({S.DRAFT, S.CHANGES_REQUESTED, S.UNPUBLISHED}, S.SUBMITTED_FOR_REVIEW, False),
    "withdraw": ({S.SUBMITTED_FOR_REVIEW}, S.DRAFT, False),
    "approve": ({S.SUBMITTED_FOR_REVIEW}, S.APPROVED, True),
    "request_changes": ({S.SUBMITTED_FOR_REVIEW, S.APPROVED}, S.CHANGES_REQUESTED, True),
    "reject": ({S.SUBMITTED_FOR_REVIEW, S.APPROVED}, S.DRAFT, True),
    "publish": ({S.APPROVED, S.UNPUBLISHED}, S.PUBLISHED, True),
    "unpublish": ({S.PUBLISHED}, S.UNPUBLISHED, True),
    "archive": ({s for s in CourseStatus if s != S.ARCHIVED}, S.ARCHIVED, True),
    "restore": ({S.ARCHIVED}, S.DRAFT, True),
}

LABELS = {
    "submit": "submitted for review",
    "withdraw": "withdrawn from review",
    "approve": "approved",
    "request_changes": "returned with requested changes",
    "reject": "rejected",
    "publish": "published",
    "unpublish": "unpublished",
    "archive": "archived",
    "restore": "restored to draft",
}


def transition(
    db: Session, course: Course, action: str, actor: User, comment: str | None, override: bool, ip: str | None
) -> Course:
    sources, target, admin_only = TRANSITIONS[action]
    if admin_only and not actor.is_admin:
        raise AppError(403, "forbidden", "Only an Admin can perform this action")
    current = course.status
    overridden = False
    if current not in sources:
        if action == "publish" and actor.is_admin and override and current != S.ARCHIVED:
            if not comment:
                raise AppError(400, "comment_required", "A reason is required to override the review workflow")
            overridden = True
        else:
            raise AppError(
                409,
                "invalid_transition",
                f"Cannot {action.replace('_', ' ')} a course that is {current.value.replace('_', ' ').lower()}",
            )
    if action in ("request_changes", "reject") and not comment:
        raise AppError(400, "comment_required", "Please explain what needs to change")
    if action in ("submit", "publish"):
        lessons = (
            db.scalar(
                select(func.count())
                .select_from(Lesson)
                .where(Lesson.course_id == course.id, Lesson.archived_at.is_(None))
            )
            or 0
        )
        if lessons == 0:
            raise AppError(400, "incomplete_course", "Add at least one lesson before submitting or publishing")
        if not course.description.strip():
            raise AppError(400, "incomplete_course", "Add a course description before submitting or publishing")
    course.status = target
    if target == S.PUBLISHED:
        course.published_at = course.published_at or datetime.now(UTC)
    db.add(
        CoursePublicationReview(
            course_id=course.id,
            actor_id=actor.id,
            action=action,
            from_status=current,
            to_status=target,
            comment=comment,
        )
    )
    audit(
        db,
        actor.id,
        f"course.{action}",
        "course",
        course.id,
        {"from": current.value, "to": target.value, "comment": comment, "override": overridden},
        ip,
    )

    label = LABELS[action]
    if action == "submit":
        notify_admins(
            db,
            "course.submitted",
            f"Course submitted: {course.title}",
            f"{actor.full_name} submitted a course for review.",
            f"/admin/reviews?course={course.id}",
        )
        from app.core.config import get_settings

        s = get_settings()
        queue_email(
            db,
            s.approval_notification_email,
            f"[DevOps Freshers] Course submitted for review: {course.title}",
            f'{actor.full_name} submitted "{course.title}" for review.\n\n'
            f"Review it here (sign-in required): {s.frontend_url}/admin/reviews?course={course.id}",
            f"course-submit:{course.id}:{datetime.now(UTC).isoformat()}",
        )
    elif course.owner_id != actor.id:
        notify(
            db,
            course.owner_id,
            f"course.{action}",
            f'"{course.title}" was {label}',
            comment,
            f"/studio/courses/{course.id}",
        )
        owner = db.get(User, course.owner_id)
        if owner:
            queue_email(
                db,
                owner.email,
                f'Your course "{course.title}" was {label}',
                f'Hi {owner.full_name},\n\nYour course "{course.title}" was {label}.'
                + (f"\n\nComments from the reviewer:\n{comment}" if comment else ""),
                f"course-{action}:{course.id}:{datetime.now(UTC).isoformat()}",
            )
    return course
