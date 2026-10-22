"""Progress rules.

Course progress = completed required lessons / total required lessons (folders never count).
Quiz lessons are complete when the student passes the published quiz; a course is complete when
progress reaches 100%.
"""

import uuid
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Enrollment, Lesson, LessonProgress, Quiz
from app.models.enums import LessonType, QuizStatus


def required_lessons_query(course_ids: list[uuid.UUID]):
    published_quiz = select(Quiz.lesson_id).where(Quiz.status == QuizStatus.PUBLISHED)
    return select(Lesson.id, Lesson.course_id).where(
        Lesson.course_id.in_(course_ids),
        Lesson.archived_at.is_(None),
        Lesson.is_required.is_(True),
        # Quiz lessons only count once their quiz is published.
        (Lesson.lesson_type != LessonType.QUIZ) | Lesson.id.in_(published_quiz),
    )


def course_progress_detail(db: Session, student_id: uuid.UUID, course_ids: list[uuid.UUID]) -> dict:
    if not course_ids:
        return {}
    required = db.execute(required_lessons_query(course_ids)).all()
    totals: dict = defaultdict(int)
    for _, cid in required:
        totals[cid] += 1
    req_ids = [lid for lid, _ in required]
    completed: dict = defaultdict(int)
    if req_ids:
        for cid, n in db.execute(
            select(LessonProgress.course_id, func.count(func.distinct(LessonProgress.lesson_id)))
            .where(
                LessonProgress.student_id == student_id,
                LessonProgress.completed.is_(True),
                LessonProgress.lesson_id.in_(req_ids),
            )
            .group_by(LessonProgress.course_id)
        ):
            completed[cid] = n
    out = {}
    for cid in course_ids:
        t, c = totals.get(cid, 0), completed.get(cid, 0)
        out[cid] = {"total": t, "completed": c, "percent": round(c * 100 / t) if t else 0}
    return out


def course_progress_map(db: Session, student_id: uuid.UUID, course_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    return {cid: d["percent"] for cid, d in course_progress_detail(db, student_id, course_ids).items()}


def get_or_create_progress(db: Session, student_id: uuid.UUID, lesson: Lesson) -> LessonProgress:
    p = db.scalar(
        select(LessonProgress)
        .where(LessonProgress.student_id == student_id, LessonProgress.lesson_id == lesson.id)
        .with_for_update(of=LessonProgress)
    )
    if not p:
        p = LessonProgress(
            student_id=student_id,
            course_id=lesson.course_id,
            lesson_id=lesson.id,
            last_position_seconds=0,
            watched_seconds=0,
            completed=False,
        )
        db.add(p)
        db.flush()
    return p


def video_completion_reached(watched: float, duration: float | None) -> bool:
    if not duration or duration <= 0:
        return False
    if duration <= 30:
        return watched >= max(duration - 2, duration * 0.8)
    return watched >= duration * get_settings().video_completion_threshold


def mark_complete(db: Session, p: LessonProgress) -> bool:
    if p.completed:
        return False
    p.completed = True
    p.completed_at = datetime.now(UTC)
    db.flush()
    _maybe_complete_course(db, p.student_id, p.course_id)
    return True


def _maybe_complete_course(db: Session, student_id: uuid.UUID, course_id: uuid.UUID) -> None:
    d = course_progress_detail(db, student_id, [course_id]).get(course_id)
    if d and d["total"] and d["completed"] >= d["total"]:
        e = db.scalar(select(Enrollment).where(Enrollment.student_id == student_id, Enrollment.course_id == course_id))
        if e and not e.completed_at:
            e.completed_at = datetime.now(UTC)
            from app.services.notify import notify

            notify(
                db,
                student_id,
                "course.completed",
                "Course completed!",
                f"Congratulations on completing {e.course.title}.",
                f"/learn/{course_id}",
            )
