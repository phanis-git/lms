import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.errors import AppError, bad_request
from app.models import Enrollment, LessonProgress, User
from app.models.enums import LessonType
from app.schemas.learning import CourseProgressOut, LessonProgressOut, ProgressIn
from app.services.access import can_manage, get_course, get_lesson, require_view_content
from app.services.catalog import ordered_lessons
from app.services.progress import (
    course_progress_detail,
    get_or_create_progress,
    mark_complete,
    video_completion_reached,
)

router = APIRouter(tags=["progress"])

# Allow a little slack over wall-clock time (network jitter, 1.25x playback is still honest-ish).
WATCH_SLACK = 2.0


def _out(p: LessonProgress) -> LessonProgressOut:
    return LessonProgressOut(
        lesson_id=p.lesson_id,
        last_position_seconds=p.last_position_seconds,
        duration_seconds=p.duration_seconds,
        watched_seconds=p.watched_seconds,
        completed=p.completed,
        completed_at=p.completed_at,
        last_activity_at=p.last_activity_at,
    )


def _touch_enrollment(db: Session, user: User, course_id: uuid.UUID) -> None:
    e = db.scalar(select(Enrollment).where(Enrollment.student_id == user.id, Enrollment.course_id == course_id))
    if e:
        e.last_accessed_at = datetime.now(UTC)


def _learner_lesson(db: Session, user: User, lesson_id: uuid.UUID):
    les = get_lesson(db, lesson_id)
    course = get_course(db, les.course_id)
    require_view_content(db, user, course)
    return les, course


@router.put("/lessons/{lesson_id}/progress", response_model=LessonProgressOut | None)
def save_progress(
    lesson_id: uuid.UUID,
    body: ProgressIn,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    les, course = _learner_lesson(db, user, lesson_id)
    if can_manage(user, course):
        response.status_code = 204  # authors previewing their course do not accumulate progress
        return None
    if les.lesson_type != LessonType.VIDEO:
        raise bad_request("Progress positions apply to video lessons only")
    p = get_or_create_progress(db, user.id, les)
    now = datetime.now(UTC)
    asset_duration = les.video_asset.duration_seconds if les.video_asset else None
    duration = asset_duration or body.duration_seconds
    if duration:
        p.duration_seconds = duration
    # Credit watched time no faster than real time has passed since the last update.
    elapsed = (now - p.last_activity_at).total_seconds() if p.last_activity_at and p.watched_seconds else 600
    credit = min(body.watched_delta_seconds, max(elapsed, 0) * WATCH_SLACK + 5)
    p.watched_seconds = min(p.watched_seconds + credit, duration or p.watched_seconds + credit)
    p.last_position_seconds = min(body.position_seconds, duration) if duration else body.position_seconds
    p.last_activity_at = now
    _touch_enrollment(db, user, les.course_id)
    if not p.completed and video_completion_reached(p.watched_seconds, p.duration_seconds):
        mark_complete(db, p)
    db.commit()
    return _out(p)


@router.post("/lessons/{lesson_id}/complete", response_model=LessonProgressOut)
def complete_lesson(lesson_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    les, course = _learner_lesson(db, user, lesson_id)
    if can_manage(user, course):
        raise bad_request("Course authors do not track progress")
    if les.lesson_type == LessonType.QUIZ:
        raise AppError(409, "quiz_required", "Pass the quiz to complete this lesson")
    p = get_or_create_progress(db, user.id, les)
    if les.lesson_type == LessonType.VIDEO and not p.completed:
        if not video_completion_reached(p.watched_seconds, p.duration_seconds):
            raise AppError(409, "not_watched", "Watch most of the recording before marking it complete")
    p.last_activity_at = datetime.now(UTC)
    mark_complete(db, p)
    _touch_enrollment(db, user, les.course_id)
    db.commit()
    return _out(p)


@router.get("/courses/{course_id}/progress", response_model=CourseProgressOut)
def course_progress(course_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    course = get_course(db, course_id)
    require_view_content(db, user, course)
    d = course_progress_detail(db, user.id, [course.id])[course.id]
    rows = db.scalars(
        select(LessonProgress).where(LessonProgress.student_id == user.id, LessonProgress.course_id == course.id)
    ).all()
    by_lesson = {p.lesson_id: p for p in rows}
    order = ordered_lessons(db, course.id)
    resume = None
    if rows:
        latest = max(rows, key=lambda p: p.last_activity_at or datetime.min.replace(tzinfo=UTC))
        resume = latest.lesson_id
        if latest.completed:
            ids = [les.id for les in order]
            if latest.lesson_id in ids:
                nxt = next(
                    (
                        les
                        for les in order[ids.index(latest.lesson_id) + 1 :]
                        if not (by_lesson.get(les.id) and by_lesson[les.id].completed)
                    ),
                    None,
                )
                resume = nxt.id if nxt else latest.lesson_id
    elif order:
        resume = order[0].id
    e = db.scalar(select(Enrollment).where(Enrollment.student_id == user.id, Enrollment.course_id == course.id))
    return CourseProgressOut(
        course_id=course.id,
        total_required=d["total"],
        completed_required=d["completed"],
        percent=d["percent"],
        completed=bool(d["total"] and d["completed"] >= d["total"]),
        completed_at=e.completed_at if e else None,
        resume_lesson_id=resume,
        lessons=[_out(p) for p in rows],
    )
