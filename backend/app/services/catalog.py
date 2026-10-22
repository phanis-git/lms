"""Builders for course summaries, public outlines and the full curriculum tree."""

import re
import unicodedata
import uuid
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Course, Folder, Lesson, LessonProgress, LessonResource, MediaAsset, Module, Quiz, User
from app.models.enums import LessonType, MediaStatus, QuizStatus, QuizUnlockRule
from app.schemas.courses import (
    CategoryOut,
    CourseSummary,
    OutlineFolder,
    OutlineLesson,
    OutlineModule,
    PersonOut,
    ResourceOut,
    TreeFolder,
    TreeLesson,
    TreeModule,
    VideoInfo,
)
from app.services import storage


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value[:100] or "item"


def unique_slug(db: Session, model, base: str) -> str:
    slug = slugify(base)
    candidate, n = slug, 2
    while db.scalar(select(model.id).where(model.slug == candidate)):
        candidate = f"{slug}-{n}"
        n += 1
    return candidate


def category_out(c, course_count: int = 0) -> CategoryOut:
    return CategoryOut(
        id=c.id,
        name=c.name,
        slug=c.slug,
        description=c.description,
        icon=c.icon,
        is_active=c.is_active,
        position=c.position,
        course_count=course_count,
    )


def person_out(u: User) -> PersonOut:
    p = u.profile
    return PersonOut(id=u.id, full_name=u.full_name, headline=p.headline if p else None, bio=p.bio if p else None)


def thumbnail_url(db: Session, course: Course, origin: str | None) -> str | None:
    if not course.thumbnail_asset_id:
        return None
    asset = db.get(MediaAsset, course.thumbnail_asset_id)
    if not asset or asset.status != MediaStatus.READY:
        return None
    return storage.signed_get_url(asset.storage_key, origin, ttl=3600)


def course_stats(db: Session, course_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    stats: dict[uuid.UUID, dict] = defaultdict(lambda: {"modules": 0, "lessons": 0, "duration": 0.0})
    if not course_ids:
        return stats
    for cid, n in db.execute(
        select(Module.course_id, func.count())
        .where(Module.course_id.in_(course_ids), Module.archived_at.is_(None))
        .group_by(Module.course_id)
    ):
        stats[cid]["modules"] = n
    for cid, n, dur in db.execute(
        select(Lesson.course_id, func.count(Lesson.id), func.coalesce(func.sum(MediaAsset.duration_seconds), 0))
        .outerjoin(MediaAsset, MediaAsset.id == Lesson.video_asset_id)
        .where(Lesson.course_id.in_(course_ids), Lesson.archived_at.is_(None))
        .group_by(Lesson.course_id)
    ):
        stats[cid]["lessons"] = n
        stats[cid]["duration"] = float(dur or 0)
    return stats


def course_summary(db: Session, course: Course, stats: dict, origin: str | None, **extra) -> CourseSummary:
    st = stats.get(course.id, {"modules": 0, "lessons": 0, "duration": 0.0})
    return CourseSummary(
        id=course.id,
        slug=course.slug,
        title=course.title,
        subtitle=course.subtitle,
        description=course.description,
        category=category_out(course.category),
        instructor=person_out(course.owner),
        price_inr=course.price_paise / 100,
        status=course.status,
        level=course.level,
        language=course.language,
        thumbnail_url=thumbnail_url(db, course, origin),
        is_featured=course.is_featured,
        module_count=st["modules"],
        lesson_count=st["lessons"],
        total_duration_seconds=st["duration"],
        published_at=course.published_at,
        updated_at=course.updated_at,
        **extra,
    )


def _load(db: Session, course_id: uuid.UUID):
    modules = db.scalars(
        select(Module)
        .where(Module.course_id == course_id, Module.archived_at.is_(None))
        .order_by(Module.position, Module.created_at)
    ).all()
    folders = db.scalars(
        select(Folder)
        .where(Folder.course_id == course_id, Folder.archived_at.is_(None))
        .order_by(Folder.position, Folder.created_at)
    ).all()
    lessons = (
        db.scalars(
            select(Lesson)
            .where(Lesson.course_id == course_id, Lesson.archived_at.is_(None))
            .order_by(Lesson.position, Lesson.created_at)
        )
        .unique()
        .all()
    )
    return modules, folders, lessons


def ordered_lessons(db: Session, course_id: uuid.UUID) -> list[Lesson]:
    """Lessons in curriculum (depth-first) order, used for next/previous navigation."""
    modules, folders, lessons = _load(db, course_id)
    by_parent: dict = defaultdict(list)
    for f in folders:
        by_parent[(f.module_id, f.parent_id)].append(f)
    lessons_by: dict = defaultdict(list)
    for les in lessons:
        lessons_by[(les.module_id, les.folder_id)].append(les)
    out: list[Lesson] = []

    def walk(module_id, folder_id):
        out.extend(lessons_by[(module_id, folder_id)])
        for child in by_parent[(module_id, folder_id)]:
            walk(module_id, child.id)

    for m in modules:
        walk(m.id, None)
    return out


def public_outline(db: Session, course_id: uuid.UUID) -> list[OutlineModule]:
    modules, folders, lessons = _load(db, course_id)

    def ol(les: Lesson) -> OutlineLesson:
        dur = les.video_asset.duration_seconds if les.video_asset else None
        return OutlineLesson(
            id=les.id,
            title=les.title,
            lesson_type=les.lesson_type,
            duration_seconds=dur,
            is_preview=les.is_preview,
            is_required=les.is_required,
        )

    return _assemble(
        modules,
        folders,
        lessons,
        ol,
        lambda f: OutlineFolder(id=f.id, title=f.title),
        lambda m: OutlineModule(id=m.id, title=m.title, description=m.description),
    )


def _assemble(modules, folders, lessons, lesson_fn, folder_fn, module_fn):
    folder_nodes = {f.id: (f, folder_fn(f)) for f in folders}
    module_nodes = {m.id: module_fn(m) for m in modules}
    for f, node in folder_nodes.values():
        if f.parent_id and f.parent_id in folder_nodes:
            folder_nodes[f.parent_id][1].folders.append(node)
        elif f.module_id in module_nodes and not f.parent_id:
            module_nodes[f.module_id].folders.append(node)
    for les in lessons:
        node = lesson_fn(les)
        if node is None:
            continue
        if les.folder_id and les.folder_id in folder_nodes:
            folder_nodes[les.folder_id][1].lessons.append(node)
        elif les.module_id in module_nodes and not les.folder_id:
            module_nodes[les.module_id].lessons.append(node)
    return [module_nodes[m.id] for m in modules]


def curriculum_tree(db: Session, course: Course, manager: bool, student: User | None) -> list[TreeModule]:
    modules, folders, lessons = _load(db, course.id)
    lesson_ids = [les.id for les in lessons]
    resources: dict = defaultdict(list)
    if lesson_ids:
        for r in db.scalars(
            select(LessonResource).where(LessonResource.lesson_id.in_(lesson_ids)).order_by(LessonResource.position)
        ).unique():
            if r.media_asset.status != MediaStatus.READY:
                continue
            resources[r.lesson_id].append(
                ResourceOut(
                    id=r.id,
                    title=r.title,
                    downloadable=r.downloadable and course.downloads_allowed,
                    filename=r.media_asset.original_filename,
                    content_type=r.media_asset.content_type,
                    size_bytes=r.media_asset.size_bytes,
                )
            )
    quizzes = {}
    if lesson_ids:
        for q in db.scalars(select(Quiz).where(Quiz.lesson_id.in_(lesson_ids), Quiz.status != QuizStatus.ARCHIVED)):
            if manager or q.status == QuizStatus.PUBLISHED:
                quizzes.setdefault(q.lesson_id, q)
    progress = {}
    if student and not manager:
        progress = {
            p.lesson_id: p
            for p in db.scalars(
                select(LessonProgress).where(
                    LessonProgress.student_id == student.id, LessonProgress.course_id == course.id
                )
            )
        }
    folder_lessons: dict = defaultdict(list)
    for les in lessons:
        folder_lessons[(les.module_id, les.folder_id)].append(les)

    def is_locked(les: Lesson) -> bool:
        q = quizzes.get(les.id)
        if manager or not q or q.unlock_rule != QuizUnlockRule.AFTER_FOLDER_LESSONS:
            return False
        siblings = [
            s
            for s in folder_lessons[(les.module_id, les.folder_id)]
            if s.id != les.id and s.is_required and s.lesson_type != LessonType.QUIZ
        ]
        return not all(progress.get(s.id) and progress[s.id].completed for s in siblings)

    def tl(les: Lesson) -> TreeLesson | None:
        if not manager and les.lesson_type == LessonType.QUIZ and les.id not in quizzes:
            return None  # quiz not published yet
        video = None
        if les.video_asset and les.video_asset.status != MediaStatus.DELETED:
            a = les.video_asset
            video = VideoInfo(
                asset_id=a.id,
                filename=a.original_filename,
                status=a.status,
                size_bytes=a.size_bytes,
                duration_seconds=a.duration_seconds,
            )
        p = progress.get(les.id)
        q = quizzes.get(les.id)
        return TreeLesson(
            id=les.id,
            module_id=les.module_id,
            folder_id=les.folder_id,
            title=les.title,
            description=les.description,
            lesson_type=les.lesson_type,
            content=les.content,
            position=les.position,
            is_required=les.is_required,
            is_preview=les.is_preview,
            video=video,
            resources=resources.get(les.id, []),
            quiz_id=q.id if q else None,
            quiz_status=q.status.value if q else None,
            completed=bool(p and p.completed),
            last_position_seconds=p.last_position_seconds if p else 0,
            locked=is_locked(les),
        )

    return _assemble(
        modules,
        folders,
        lessons,
        tl,
        lambda f: TreeFolder(id=f.id, module_id=f.module_id, parent_id=f.parent_id, title=f.title, position=f.position),
        lambda m: TreeModule(id=m.id, title=m.title, description=m.description, position=m.position),
    )
