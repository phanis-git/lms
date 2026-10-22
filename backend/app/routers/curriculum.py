import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import client_ip, get_current_user, public_origin, require_author
from app.core.errors import AppError, bad_request, forbidden, not_found
from app.models import Folder, Lesson, LessonResource, MediaAsset, Module, Quiz, User
from app.models.enums import LessonType, MediaKind, MediaStatus
from app.schemas.common import Message
from app.schemas.courses import (
    FolderIn,
    FolderOut,
    FolderUpdate,
    LessonIn,
    LessonOut,
    LessonUpdate,
    ModuleIn,
    ModuleOut,
    ModuleUpdate,
    ReorderIn,
    ResourceIn,
    ResourceOut,
    ResourceUpdate,
)
from app.services import storage
from app.services.access import (
    can_manage,
    get_course,
    get_folder,
    get_lesson,
    get_module,
    require_edit,
    require_view_content,
)
from app.services.audit import audit

router = APIRouter(tags=["curriculum"])


def _next_position(db: Session, model, **filters) -> int:
    stmt = select(func.coalesce(func.max(model.position), -1))
    for k, v in filters.items():
        col = getattr(model, k)
        stmt = stmt.where(col.is_(None) if v is None else col == v)
    stmt = stmt.where(model.archived_at.is_(None))
    return int(db.scalar(stmt)) + 1


def _edit_course(db: Session, user: User, course_id: uuid.UUID):
    course = get_course(db, course_id)
    require_edit(user, course)
    return course


# ---------- Modules ----------
@router.post("/courses/{course_id}/modules", response_model=ModuleOut, status_code=201)
def create_module(
    course_id: uuid.UUID,
    body: ModuleIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_author),
):
    course = _edit_course(db, user, course_id)
    m = Module(
        course_id=course.id,
        title=body.title.strip(),
        description=body.description,
        position=_next_position(db, Module, course_id=course.id),
    )
    db.add(m)
    db.flush()
    audit(
        db,
        user.id,
        "module.created",
        "module",
        m.id,
        {"course_id": str(course.id), "title": m.title},
        client_ip(request),
    )
    db.commit()
    return m


@router.patch("/modules/{module_id}", response_model=ModuleOut)
def update_module(
    module_id: uuid.UUID,
    body: ModuleUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_author),
):
    m = get_module(db, module_id)
    _edit_course(db, user, m.course_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        if k == "title" and not v:
            continue
        setattr(m, k, v)
    audit(db, user.id, "module.updated", "module", m.id, body.model_dump(exclude_unset=True), client_ip(request))
    db.commit()
    return m


@router.delete("/modules/{module_id}", response_model=Message)
def archive_module(
    module_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(require_author)
):
    m = get_module(db, module_id)
    _edit_course(db, user, m.course_id)
    now = datetime.now(UTC)
    m.archived_at = now
    # Soft-delete children so historical progress rows remain valid.
    for f in db.scalars(select(Folder).where(Folder.module_id == m.id, Folder.archived_at.is_(None))):
        f.archived_at = now
    for les in db.scalars(select(Lesson).where(Lesson.module_id == m.id, Lesson.archived_at.is_(None))):
        les.archived_at = now
    audit(db, user.id, "module.archived", "module", m.id, {"title": m.title}, client_ip(request))
    db.commit()
    return Message(message="Module removed")


# ---------- Folders ----------
def _assert_no_cycle(db: Session, folder: Folder, new_parent: Folder | None) -> None:
    node = new_parent
    depth = 0
    while node is not None:
        if node.id == folder.id:
            raise bad_request("A folder cannot be moved inside itself or one of its subfolders", "folder_cycle")
        depth += 1
        if depth > 50:
            raise bad_request("Folders are nested too deeply")
        node = db.get(Folder, node.parent_id) if node.parent_id else None


def _descendant_folder_ids(db: Session, folder_id: uuid.UUID) -> list[uuid.UUID]:
    ids, frontier = [], [folder_id]
    while frontier:
        children = db.scalars(
            select(Folder.id).where(Folder.parent_id.in_(frontier), Folder.archived_at.is_(None))
        ).all()
        ids.extend(children)
        frontier = list(children)
    return ids


@router.post("/modules/{module_id}/folders", response_model=FolderOut, status_code=201)
def create_folder(
    module_id: uuid.UUID,
    body: FolderIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_author),
):
    m = get_module(db, module_id)
    _edit_course(db, user, m.course_id)
    if body.parent_id:
        parent = get_folder(db, body.parent_id)
        if parent.module_id != m.id:
            raise bad_request("Parent folder belongs to a different module")
    f = Folder(
        course_id=m.course_id,
        module_id=m.id,
        parent_id=body.parent_id,
        title=body.title.strip(),
        position=_next_position(db, Folder, module_id=m.id, parent_id=body.parent_id),
    )
    db.add(f)
    db.flush()
    audit(db, user.id, "folder.created", "folder", f.id, {"title": f.title}, client_ip(request))
    db.commit()
    return f


@router.patch("/folders/{folder_id}", response_model=FolderOut)
def update_folder(
    folder_id: uuid.UUID,
    body: FolderUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_author),
):
    f = get_folder(db, folder_id)
    _edit_course(db, user, f.course_id)
    if body.title:
        f.title = body.title.strip()
    if body.move:
        target_module = get_module(db, body.module_id) if body.module_id else get_module(db, f.module_id)
        if target_module.course_id != f.course_id:
            raise bad_request("Folders can only be moved within the same course")
        parent = get_folder(db, body.parent_id) if body.parent_id else None
        if parent and parent.module_id != target_module.id:
            raise bad_request("Parent folder belongs to a different module")
        _assert_no_cycle(db, f, parent)
        if target_module.id != f.module_id:
            sub = _descendant_folder_ids(db, f.id)
            for child in db.scalars(select(Folder).where(Folder.id.in_(sub))):
                child.module_id = target_module.id
            for les in db.scalars(select(Lesson).where(Lesson.folder_id.in_([f.id, *sub]))):
                les.module_id = target_module.id
        f.module_id = target_module.id
        f.parent_id = parent.id if parent else None
        f.position = _next_position(db, Folder, module_id=f.module_id, parent_id=f.parent_id)
    audit(
        db,
        user.id,
        "folder.updated",
        "folder",
        f.id,
        {"title": f.title, "moved": body.move, "parent_id": str(f.parent_id) if f.parent_id else None},
        client_ip(request),
    )
    db.commit()
    return f


@router.delete("/folders/{folder_id}", response_model=Message)
def archive_folder(
    folder_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(require_author)
):
    f = get_folder(db, folder_id)
    _edit_course(db, user, f.course_id)
    now = datetime.now(UTC)
    ids = [f.id, *_descendant_folder_ids(db, f.id)]
    for folder in db.scalars(select(Folder).where(Folder.id.in_(ids))):
        folder.archived_at = now
    for les in db.scalars(select(Lesson).where(Lesson.folder_id.in_(ids), Lesson.archived_at.is_(None))):
        les.archived_at = now
    audit(db, user.id, "folder.archived", "folder", f.id, {"title": f.title}, client_ip(request))
    db.commit()
    return Message(message="Folder removed")


# ---------- Lessons ----------
def _check_video(db: Session, user: User, course_id: uuid.UUID, asset_id: uuid.UUID) -> MediaAsset:
    a = db.get(MediaAsset, asset_id)
    if not a or a.kind != MediaKind.VIDEO or a.status not in (MediaStatus.READY, MediaStatus.PROCESSING):
        raise bad_request("The video upload is missing or not finished")
    if a.course_id != course_id:
        raise bad_request("The video belongs to a different course")
    if a.owner_id != user.id and not user.is_admin:
        raise forbidden()
    return a


@router.post("/lessons", response_model=LessonOut, status_code=201)
def create_lesson(
    body: LessonIn, request: Request, db: Session = Depends(get_db), user: User = Depends(require_author)
):
    m = get_module(db, body.module_id)
    _edit_course(db, user, m.course_id)
    if body.folder_id:
        f = get_folder(db, body.folder_id)
        if f.module_id != m.id:
            raise bad_request("Folder belongs to a different module")
    les = Lesson(
        course_id=m.course_id,
        module_id=m.id,
        folder_id=body.folder_id,
        title=body.title.strip(),
        description=body.description,
        lesson_type=body.lesson_type,
        content=body.content,
        is_required=body.is_required,
        is_preview=body.is_preview,
        position=_next_position(db, Lesson, module_id=m.id, folder_id=body.folder_id),
    )
    db.add(les)
    db.flush()
    if body.lesson_type == LessonType.QUIZ:
        db.add(Quiz(lesson_id=les.id, course_id=les.course_id, title=les.title))
    audit(
        db,
        user.id,
        "lesson.created",
        "lesson",
        les.id,
        {"title": les.title, "type": les.lesson_type.value},
        client_ip(request),
    )
    db.commit()
    return les


@router.get("/lessons/{lesson_id}", response_model=LessonOut)
def get_lesson_detail(lesson_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    les = get_lesson(db, lesson_id)
    course = get_course(db, les.course_id)
    require_view_content(db, user, course)
    return les


@router.patch("/lessons/{lesson_id}", response_model=LessonOut)
def update_lesson(
    lesson_id: uuid.UUID,
    body: LessonUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_author),
):
    les = get_lesson(db, lesson_id)
    _edit_course(db, user, les.course_id)
    data = body.model_dump(exclude_unset=True, exclude={"move", "clear_video", "module_id", "folder_id"})
    if "video_asset_id" in data:
        vid = data.pop("video_asset_id")
        if vid:
            if les.lesson_type != LessonType.VIDEO:
                raise bad_request("Only video lessons can have a recording")
            _check_video(db, user, les.course_id, vid)
            # Replacing the recording keeps the lesson id, so progress history stays attached.
            old = les.video_asset_id
            les.video_asset_id = vid
            audit(
                db,
                user.id,
                "lesson.video_replaced" if old else "lesson.video_attached",
                "lesson",
                les.id,
                {"old_asset": str(old) if old else None, "new_asset": str(vid)},
                client_ip(request),
            )
    if body.clear_video:
        les.video_asset_id = None
    if body.move:
        m = get_module(db, body.module_id or les.module_id)
        if m.course_id != les.course_id:
            raise bad_request("Lessons can only be moved within the same course")
        if body.folder_id:
            f = get_folder(db, body.folder_id)
            if f.module_id != m.id:
                raise bad_request("Folder belongs to a different module")
        les.module_id, les.folder_id = m.id, body.folder_id
        les.position = _next_position(db, Lesson, module_id=m.id, folder_id=body.folder_id)
    for k, v in data.items():
        if k == "title" and not v:
            continue
        setattr(les, k, v)
    audit(
        db,
        user.id,
        "lesson.updated",
        "lesson",
        les.id,
        {k: v for k, v in data.items() if k != "content"},
        client_ip(request),
    )
    db.commit()
    db.refresh(les)
    return les


@router.delete("/lessons/{lesson_id}", response_model=Message)
def archive_lesson(
    lesson_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(require_author)
):
    les = get_lesson(db, lesson_id)
    _edit_course(db, user, les.course_id)
    les.archived_at = datetime.now(UTC)
    audit(db, user.id, "lesson.archived", "lesson", les.id, {"title": les.title}, client_ip(request))
    db.commit()
    return Message(message="Lesson removed")


@router.post("/courses/{course_id}/reorder", response_model=Message)
def reorder(
    course_id: uuid.UUID,
    body: ReorderIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_author),
):
    _edit_course(db, user, course_id)
    model = {"module": Module, "folder": Folder, "lesson": Lesson}[body.kind]
    items = {i.id: i for i in db.scalars(select(model).where(model.id.in_(body.ids))).unique()}
    if len(items) != len(set(body.ids)) or any(i.course_id != course_id for i in items.values()):
        raise bad_request("All items must belong to this course")
    parents = {
        (getattr(i, "module_id", None), getattr(i, "parent_id", None), getattr(i, "folder_id", None))
        for i in items.values()
    }
    if body.kind != "module" and len(parents) > 1:
        raise bad_request("Only siblings can be reordered together")
    for pos, item_id in enumerate(body.ids):
        items[item_id].position = pos
    audit(db, user.id, f"{body.kind}.reordered", "course", course_id, {"count": len(body.ids)}, client_ip(request))
    db.commit()
    return Message(message="Order saved")


# ---------- Resources ----------
@router.post("/lessons/{lesson_id}/resources", response_model=ResourceOut, status_code=201)
def add_resource(
    lesson_id: uuid.UUID,
    body: ResourceIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_author),
):
    les = get_lesson(db, lesson_id)
    _edit_course(db, user, les.course_id)
    a = db.get(MediaAsset, body.media_asset_id)
    if not a or a.status != MediaStatus.READY or a.course_id != les.course_id:
        raise bad_request("The file upload is missing, unfinished, or belongs to another course")
    if a.owner_id != user.id and not user.is_admin:
        raise forbidden()
    pos = (db.scalar(select(func.max(LessonResource.position)).where(LessonResource.lesson_id == les.id)) or 0) + 1
    r = LessonResource(
        lesson_id=les.id, media_asset_id=a.id, title=body.title.strip(), downloadable=body.downloadable, position=pos
    )
    db.add(r)
    db.flush()
    audit(
        db,
        user.id,
        "resource.added",
        "lesson",
        les.id,
        {"resource_id": str(r.id), "title": r.title},
        client_ip(request),
    )
    db.commit()
    return ResourceOut(
        id=r.id,
        title=r.title,
        downloadable=r.downloadable,
        filename=a.original_filename,
        content_type=a.content_type,
        size_bytes=a.size_bytes,
    )


def _resource(db: Session, resource_id: uuid.UUID) -> tuple[LessonResource, Lesson]:
    r = db.get(LessonResource, resource_id)
    if not r:
        raise not_found("Resource")
    return r, get_lesson(db, r.lesson_id)


@router.patch("/resources/{resource_id}", response_model=ResourceOut)
def update_resource(
    resource_id: uuid.UUID, body: ResourceUpdate, db: Session = Depends(get_db), user: User = Depends(require_author)
):
    r, les = _resource(db, resource_id)
    _edit_course(db, user, les.course_id)
    if body.title:
        r.title = body.title.strip()
    if body.downloadable is not None:
        r.downloadable = body.downloadable
    db.commit()
    a = r.media_asset
    return ResourceOut(
        id=r.id,
        title=r.title,
        downloadable=r.downloadable,
        filename=a.original_filename,
        content_type=a.content_type,
        size_bytes=a.size_bytes,
    )


@router.delete("/resources/{resource_id}", response_model=Message)
def delete_resource(
    resource_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(require_author)
):
    r, les = _resource(db, resource_id)
    _edit_course(db, user, les.course_id)
    db.delete(r)
    audit(db, user.id, "resource.removed", "lesson", les.id, {"resource_id": str(resource_id)}, client_ip(request))
    db.commit()
    return Message(message="Resource removed")


@router.get("/resources/{resource_id}/download")
def download_resource(
    resource_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    r, les = _resource(db, resource_id)
    course = get_course(db, les.course_id)
    require_view_content(db, user, course)
    manager = can_manage(user, course)
    if not manager and not (course.downloads_allowed and r.downloadable):
        raise AppError(403, "download_disabled", "Downloads are disabled for this resource")
    a = r.media_asset
    if a.status != MediaStatus.READY:
        raise not_found("Resource")
    url = storage.signed_get_url(
        a.storage_key, public_origin(request), filename=a.original_filename, download=True, ttl=300
    )
    return {"url": url, "expires_in": 300, "filename": a.original_filename}
