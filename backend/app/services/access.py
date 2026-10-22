"""Central authorisation helpers. Every content endpoint goes through these checks."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, forbidden, not_found
from app.models import Course, Enrollment, Folder, Lesson, Module, User
from app.models.enums import CourseStatus, EnrollmentStatus, Role

# States in which already-enrolled students keep access (unpublishing must not revoke paid access).
ENROLLED_ACCESS_STATUSES = {CourseStatus.PUBLISHED, CourseStatus.UNPUBLISHED, CourseStatus.ARCHIVED}
INSTRUCTOR_EDITABLE = {
    CourseStatus.DRAFT,
    CourseStatus.CHANGES_REQUESTED,
    CourseStatus.APPROVED,
    CourseStatus.PUBLISHED,
    CourseStatus.UNPUBLISHED,
}


def get_course(db: Session, course_id: uuid.UUID, for_update: bool = False) -> Course:
    stmt = select(Course).where(Course.id == course_id, Course.deleted_at.is_(None))
    if for_update:
        stmt = stmt.with_for_update(of=Course)
    course = db.scalar(stmt)
    if not course:
        raise not_found("Course")
    return course


def can_manage(user: User | None, course: Course) -> bool:
    if not user:
        return False
    if user.role == Role.ADMIN:
        return True
    return user.role == Role.INSTRUCTOR and course.owner_id == user.id


def require_manage(user: User, course: Course) -> None:
    """Owner instructor or Admin. Returns 404 to non-owners so private course ids are not confirmed."""
    if not can_manage(user, course):
        if course.status == CourseStatus.PUBLISHED:
            raise forbidden()
        raise not_found("Course")


def require_edit(user: User, course: Course) -> None:
    require_manage(user, course)
    if user.role == Role.ADMIN:
        if course.status == CourseStatus.ARCHIVED:
            raise AppError(409, "course_archived", "Archived courses cannot be edited")
        return
    if course.status not in INSTRUCTOR_EDITABLE:
        raise AppError(
            409,
            "course_locked",
            "This course is locked while it is under review or archived.",
        )


def active_enrollment(db: Session, user: User | None, course_id: uuid.UUID) -> Enrollment | None:
    if not user:
        return None
    return db.scalar(
        select(Enrollment).where(
            Enrollment.student_id == user.id,
            Enrollment.course_id == course_id,
            Enrollment.status == EnrollmentStatus.ACTIVE,
        )
    )


def can_view_content(db: Session, user: User | None, course: Course) -> bool:
    if can_manage(user, course):
        return True
    if course.status not in ENROLLED_ACCESS_STATUSES:
        return False
    return active_enrollment(db, user, course.id) is not None


def require_view_content(db: Session, user: User | None, course: Course) -> None:
    if not user:
        raise AppError(401, "unauthorized", "Please sign in to continue")
    if not can_view_content(db, user, course):
        if course.status == CourseStatus.PUBLISHED:
            raise AppError(403, "not_enrolled", "Purchase this course to access its content")
        raise not_found("Course")


def get_module(db: Session, module_id: uuid.UUID) -> Module:
    m = db.scalar(select(Module).where(Module.id == module_id, Module.archived_at.is_(None)))
    if not m:
        raise not_found("Module")
    return m


def get_folder(db: Session, folder_id: uuid.UUID) -> Folder:
    f = db.scalar(select(Folder).where(Folder.id == folder_id, Folder.archived_at.is_(None)))
    if not f:
        raise not_found("Folder")
    return f


def get_lesson(db: Session, lesson_id: uuid.UUID) -> Lesson:
    lesson = db.scalar(select(Lesson).where(Lesson.id == lesson_id, Lesson.archived_at.is_(None)))
    if not lesson:
        raise not_found("Lesson")
    return lesson
