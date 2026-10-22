import math
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import client_ip, get_current_user, public_origin, require_author
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.models import Course, Lesson, LessonResource, MediaAsset, PaymentRecord, User
from app.models.enums import LessonType, MediaKind, MediaStatus
from app.schemas.common import Message
from app.schemas.media import (
    AssetIdIn,
    MediaAssetOut,
    PlaybackOut,
    UploadCompleteIn,
    UploadInitiateIn,
    UploadInitiateOut,
)
from app.services import storage
from app.services.access import can_manage, can_view_content, get_course, require_edit
from app.services.audit import audit
from app.services.filetypes import resolve_type, signature_ok

router = APIRouter(prefix="/media", tags=["media"])


def _max_bytes(kind: MediaKind) -> int:
    s = get_settings()
    mb = {
        MediaKind.VIDEO: s.max_video_upload_mb,
        MediaKind.DOCUMENT: s.max_resource_upload_mb,
        MediaKind.IMAGE: s.max_image_upload_mb,
    }[kind]
    return mb * 1024 * 1024


def _asset_out(a: MediaAsset) -> MediaAssetOut:
    return MediaAssetOut(
        id=a.id,
        kind=a.kind,
        status=a.status,
        original_filename=a.original_filename,
        content_type=a.content_type,
        size_bytes=a.size_bytes,
        duration_seconds=a.duration_seconds,
        created_at=a.created_at,
    )


def _storage_key(course_id: uuid.UUID | None, purpose: str, asset_id: uuid.UUID, filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    scope = f"courses/{course_id}" if course_id else "platform"
    return f"{scope}/{purpose}/{asset_id}.{ext[:10]}"


@router.post("/upload-initiate", response_model=UploadInitiateOut, status_code=201)
def upload_initiate(
    body: UploadInitiateIn, request: Request, db: Session = Depends(get_db), user: User = Depends(require_author)
):
    if not body.course_id:
        raise bad_request("course_id is required for course media")
    course = get_course(db, body.course_id)
    require_edit(user, course)
    content_type = resolve_type(body.filename, body.kind)
    if not content_type:
        raise AppError(415, "unsupported_file_type", "This file type is not allowed")
    if body.size_bytes > _max_bytes(body.kind):
        raise AppError(413, "file_too_large", f"File exceeds the {_max_bytes(body.kind) // (1024 * 1024)} MB limit")
    asset_id = uuid.uuid4()
    key = _storage_key(course.id, body.purpose, asset_id, body.filename)
    upload_id = storage.start_multipart(key, content_type)
    part_size = storage.part_size_for(body.size_bytes)
    parts = max(1, math.ceil(body.size_bytes / part_size))
    a = MediaAsset(
        id=asset_id,
        owner_id=user.id,
        course_id=course.id,
        kind=body.kind,
        purpose=body.purpose,
        status=MediaStatus.PENDING_UPLOAD,
        original_filename=body.filename[:255],
        content_type=content_type,
        size_bytes=body.size_bytes,
        storage_key=key,
        multipart_upload_id=upload_id,
    )
    db.add(a)
    audit(
        db,
        user.id,
        "media.upload_started",
        "media",
        a.id,
        {"course_id": str(course.id), "filename": a.original_filename, "size": a.size_bytes},
        client_ip(request),
    )
    db.commit()
    urls = storage.presign_parts(key, upload_id, parts, public_origin(request))
    return UploadInitiateOut(
        asset_id=a.id, part_size=part_size, part_urls=urls, expires_in=get_settings().upload_url_ttl_seconds
    )


def _owned_asset(db: Session, user: User, asset_id: uuid.UUID) -> MediaAsset:
    a = db.get(MediaAsset, asset_id)
    if not a or a.status == MediaStatus.DELETED:
        raise not_found("Media")
    if a.owner_id != user.id and not user.is_admin:
        raise not_found("Media")
    return a


@router.post("/upload-complete", response_model=MediaAssetOut)
def upload_complete(
    body: UploadCompleteIn, request: Request, db: Session = Depends(get_db), user: User = Depends(require_author)
):
    a = _owned_asset(db, user, body.asset_id)
    if a.status != MediaStatus.PENDING_UPLOAD or not a.multipart_upload_id:
        raise conflict("This upload is already complete")
    try:
        storage.complete_multipart(
            a.storage_key,
            a.multipart_upload_id,
            [p.model_dump() for p in sorted(body.parts, key=lambda p: p.part_number)],
        )
    except Exception:
        a.status = MediaStatus.FAILED
        a.error_message = "Upload could not be completed"
        db.commit()
        raise bad_request("Upload could not be completed. Please retry the upload.", "upload_failed")
    meta = storage.head(a.storage_key)
    ok = meta is not None and int(meta["ContentLength"]) == a.size_bytes
    if ok:
        ok = signature_ok(a.content_type, storage.read_prefix(a.storage_key))
    if not ok:
        storage.delete_object(a.storage_key)
        a.status = MediaStatus.FAILED
        a.error_message = "File content did not match its type or size"
        db.commit()
        raise AppError(415, "invalid_file", "The uploaded file does not match its declared type or size")
    a.multipart_upload_id = None
    a.uploaded_at = datetime.now(UTC)
    # Videos are playable immediately; the worker fills in duration/thumbnail asynchronously.
    a.status = MediaStatus.READY
    audit(db, user.id, "media.uploaded", "media", a.id, {"filename": a.original_filename}, client_ip(request))
    db.commit()
    return _asset_out(a)


@router.post("/upload-abort", response_model=Message)
def upload_abort(body: AssetIdIn, db: Session = Depends(get_db), user: User = Depends(require_author)):
    a = _owned_asset(db, user, body.asset_id)
    if a.status == MediaStatus.PENDING_UPLOAD and a.multipart_upload_id:
        storage.abort_multipart(a.storage_key, a.multipart_upload_id)
        a.status = MediaStatus.FAILED
        a.error_message = "Upload cancelled"
        db.commit()
    return Message(message="Upload cancelled")


@router.post("/upload-direct", response_model=MediaAssetOut, status_code=201)
def upload_direct(
    request: Request,
    file: UploadFile = File(...),
    purpose: str = Form(..., pattern="^(thumbnail|payment_screenshot|upi_qr)$"),
    course_id: uuid.UUID | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Small images only (<= MAX_IMAGE_UPLOAD_MB). Large files must use the presigned multipart flow."""
    if purpose == "upi_qr" and not user.is_admin:
        raise forbidden()
    if purpose == "thumbnail":
        if not user.can_author or not course_id:
            raise forbidden()
        require_edit(user, get_course(db, course_id))
    elif course_id and purpose != "payment_screenshot":
        raise bad_request("course_id not allowed for this purpose")
    content_type = resolve_type(file.filename or "", MediaKind.IMAGE)
    if not content_type:
        raise AppError(415, "unsupported_file_type", "Upload a PNG, JPEG, WEBP or GIF image")
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    if size == 0 or size > _max_bytes(MediaKind.IMAGE):
        raise AppError(413, "file_too_large", f"Images must be under {get_settings().max_image_upload_mb} MB")
    if not signature_ok(content_type, file.file.read(64)):
        raise AppError(415, "invalid_file", "The file is not a valid image")
    file.file.seek(0)
    asset_id = uuid.uuid4()
    scope_course = course_id if purpose == "thumbnail" else None
    key = (
        _storage_key(scope_course, purpose, asset_id, file.filename or "image")
        if purpose != "payment_screenshot"
        else f"payments/{user.id}/{asset_id}.{(file.filename or 'x.png').rsplit('.', 1)[-1].lower()[:5]}"
    )
    storage.put_object(key, file.file, content_type)
    a = MediaAsset(
        id=asset_id,
        owner_id=user.id,
        course_id=scope_course,
        kind=MediaKind.IMAGE,
        purpose=purpose,
        status=MediaStatus.READY,
        original_filename=(file.filename or "image")[:255],
        content_type=content_type,
        size_bytes=size,
        storage_key=key,
        uploaded_at=datetime.now(UTC),
    )
    db.add(a)
    db.commit()
    return _asset_out(a)


@router.get("/course/{course_id}", response_model=list[MediaAssetOut])
def list_course_media(course_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_author)):
    course = get_course(db, course_id)
    if not can_manage(user, course):
        raise not_found("Course")
    rows = db.scalars(
        select(MediaAsset)
        .where(MediaAsset.course_id == course.id, MediaAsset.status.in_([MediaStatus.READY, MediaStatus.PROCESSING]))
        .order_by(MediaAsset.created_at.desc())
    ).all()
    return [_asset_out(a) for a in rows]


def _authorize_view(db: Session, user: User, a: MediaAsset) -> None:
    if a.owner_id == user.id or user.is_admin:
        return
    if a.purpose == "upi_qr":
        return  # any signed-in user may view the payment QR
    if a.purpose == "payment_screenshot":
        raise not_found("Media")
    course = db.get(Course, a.course_id) if a.course_id else None
    if not course or course.deleted_at:
        raise not_found("Media")
    if can_manage(user, course):
        return
    if a.purpose == "thumbnail":
        return
    used_by_lesson = db.scalar(
        select(Lesson).where(
            Lesson.course_id == course.id,
            Lesson.archived_at.is_(None),
            or_(
                Lesson.video_asset_id == a.id,
                Lesson.id.in_(select(LessonResource.lesson_id).where(LessonResource.media_asset_id == a.id)),
            ),
        )
    )
    if not used_by_lesson:
        raise not_found("Media")
    if used_by_lesson.is_preview and used_by_lesson.lesson_type == LessonType.VIDEO and course.is_purchasable:
        return
    if not can_view_content(db, user, course):
        raise AppError(403, "not_enrolled", "Purchase this course to watch this lesson")


@router.get("/{asset_id}/playback", response_model=PlaybackOut)
def playback(
    asset_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    a = db.get(MediaAsset, asset_id)
    if not a or a.status not in (MediaStatus.READY, MediaStatus.PROCESSING):
        raise not_found("Media")
    _authorize_view(db, user, a)
    s = get_settings()
    origin = public_origin(request)
    url = storage.signed_get_url(a.storage_key, origin)
    thumb = storage.signed_get_url(a.thumbnail_key, origin) if a.thumbnail_key else None
    return PlaybackOut(
        url=url,
        expires_in=s.playback_url_ttl_seconds,
        content_type=a.content_type,
        duration_seconds=a.duration_seconds,
        thumbnail_url=thumb,
    )


@router.delete("/{asset_id}", response_model=Message)
def delete_media(
    asset_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(require_author)
):
    a = _owned_asset(db, user, asset_id)
    if a.course_id:
        require_edit(user, get_course(db, a.course_id))
    in_use = (
        db.scalar(select(Lesson.id).where(Lesson.video_asset_id == a.id, Lesson.archived_at.is_(None)))
        or db.scalar(select(LessonResource.id).where(LessonResource.media_asset_id == a.id))
        or db.scalar(select(Course.id).where(Course.thumbnail_asset_id == a.id))
        or db.scalar(select(PaymentRecord.id).where(PaymentRecord.screenshot_asset_id == a.id))
    )
    if in_use:
        raise conflict("This file is still attached to a lesson, resource or course. Detach it first.")
    if a.multipart_upload_id:
        storage.abort_multipart(a.storage_key, a.multipart_upload_id)
    storage.delete_object(a.storage_key)
    if a.thumbnail_key:
        storage.delete_object(a.thumbnail_key)
    a.status = MediaStatus.DELETED
    audit(db, user.id, "media.deleted", "media", a.id, {"filename": a.original_filename}, client_ip(request))
    db.commit()
    return Message(message="File deleted")
