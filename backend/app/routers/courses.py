import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import client_ip, get_optional_user, public_origin, require_admin, require_author
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.models import Category, Course, CoursePublicationReview, Enrollment, MediaAsset, PaymentRecord, User
from app.models.enums import CourseStatus, EnrollmentStatus, MediaKind, MediaStatus, PaymentStatus, Role
from app.schemas.common import Message, Page
from app.schemas.courses import (
    CourseDetail,
    CourseIn,
    CourseSummary,
    CourseUpdate,
    CurriculumOut,
    PublicationReviewOut,
    ReviewActionIn,
)
from app.services import lifecycle
from app.services.access import can_manage, can_view_content, get_course, require_edit, require_manage
from app.services.audit import audit
from app.services.catalog import course_stats, course_summary, curriculum_tree, public_outline, unique_slug
from app.services.pagination import PageParams, page_params, paginate
from app.services.progress import course_progress_map

router = APIRouter(tags=["courses"])


def _resolve_course(db: Session, ref: str) -> Course:
    try:
        cid = uuid.UUID(ref)
        stmt = select(Course).where(Course.id == cid)
    except ValueError:
        stmt = select(Course).where(Course.slug == ref)
    course = db.scalar(stmt.where(Course.deleted_at.is_(None)))
    if not course:
        raise not_found("Course")
    return course


@router.get("/courses", response_model=Page[CourseSummary])
def catalog(
    request: Request,
    q: str | None = Query(None, max_length=100),
    category: str | None = Query(None, max_length=120, description="Category slug or id"),
    featured: bool | None = None,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    stmt = (
        select(Course)
        .where(Course.status == CourseStatus.PUBLISHED, Course.deleted_at.is_(None))
        .order_by(Course.is_featured.desc(), Course.published_at.desc())
    )
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(Course.title).like(like), func.lower(Course.subtitle).like(like)))
    if category:
        try:
            stmt = stmt.where(Course.category_id == uuid.UUID(category))
        except ValueError:
            stmt = stmt.join(Category, Category.id == Course.category_id).where(Category.slug == category)
    if featured is not None:
        stmt = stmt.where(Course.is_featured.is_(featured))
    rows, total = paginate(db, stmt, params)
    stats = course_stats(db, [c.id for c in rows])
    enrolled, progress = {}, {}
    if user and rows:
        enrolled = {
            e.course_id: e.status.value
            for e in db.scalars(
                select(Enrollment).where(
                    Enrollment.student_id == user.id, Enrollment.course_id.in_([c.id for c in rows])
                )
            )
        }
        progress = course_progress_map(db, user.id, [cid for cid, s in enrolled.items() if s == "ACTIVE"])
    origin = public_origin(request)
    items = [
        course_summary(db, c, stats, origin, enrollment_status=enrolled.get(c.id), progress_percent=progress.get(c.id))
        for c in rows
    ]
    return Page(items=items, total=total, page=params.page, page_size=params.page_size)


@router.get("/courses/manage", response_model=Page[CourseSummary])
def manage_list(
    request: Request,
    status: CourseStatus | None = None,
    q: str | None = Query(None, max_length=100),
    owner_id: uuid.UUID | None = None,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
    user: User = Depends(require_author),
):
    stmt = select(Course).where(Course.deleted_at.is_(None)).order_by(Course.updated_at.desc())
    if user.role != Role.ADMIN:
        stmt = stmt.where(Course.owner_id == user.id)
    elif owner_id:
        stmt = stmt.where(Course.owner_id == owner_id)
    if status:
        stmt = stmt.where(Course.status == status)
    if q:
        stmt = stmt.where(func.lower(Course.title).like(f"%{q.strip().lower()}%"))
    rows, total = paginate(db, stmt, params)
    stats = course_stats(db, [c.id for c in rows])
    origin = public_origin(request)
    return Page(
        items=[course_summary(db, c, stats, origin) for c in rows],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/courses/{ref}", response_model=CourseDetail)
def course_detail(
    ref: str, request: Request, db: Session = Depends(get_db), user: User | None = Depends(get_optional_user)
):
    course = _resolve_course(db, ref)
    manager = can_manage(user, course)
    content = can_view_content(db, user, course)
    if course.status != CourseStatus.PUBLISHED and not (manager or content):
        raise not_found("Course")
    stats = course_stats(db, [course.id])
    enrollment = None
    pending = False
    progress = None
    if user:
        enrollment = db.scalar(
            select(Enrollment).where(Enrollment.student_id == user.id, Enrollment.course_id == course.id)
        )
        pending = (
            db.scalar(
                select(PaymentRecord.id).where(
                    PaymentRecord.student_id == user.id,
                    PaymentRecord.course_id == course.id,
                    PaymentRecord.status == PaymentStatus.PENDING_VERIFICATION,
                )
            )
            is not None
        )
        if enrollment and enrollment.status == EnrollmentStatus.ACTIVE:
            progress = course_progress_map(db, user.id, [course.id]).get(course.id)
    summary = course_summary(
        db,
        course,
        stats,
        public_origin(request),
        enrollment_status=enrollment.status.value if enrollment else None,
        progress_percent=progress,
    )
    outline = public_outline(db, course.id) if (course.curriculum_public or manager or content) else None
    return CourseDetail(
        **summary.model_dump(),
        learning_outcomes=course.learning_outcomes,
        prerequisites=course.prerequisites,
        curriculum_public=course.curriculum_public,
        downloads_allowed=course.downloads_allowed,
        can_manage=manager,
        can_access_content=content,
        is_purchasable=course.is_purchasable,
        outline=outline,
        thumbnail_asset_id=course.thumbnail_asset_id if manager else None,
        pending_payment=pending,
    )


@router.get("/courses/{course_id}/curriculum", response_model=CurriculumOut)
def curriculum(course_id: uuid.UUID, db: Session = Depends(get_db), user: User | None = Depends(get_optional_user)):
    from app.services.access import require_view_content

    course = get_course(db, course_id)
    require_view_content(db, user, course)
    manager = can_manage(user, course)
    enrollment = db.scalar(
        select(Enrollment).where(Enrollment.student_id == user.id, Enrollment.course_id == course.id)
    )
    if enrollment:
        enrollment.last_accessed_at = datetime.now(UTC)
        db.commit()
    return CurriculumOut(
        course_id=course.id,
        course_title=course.title,
        course_status=course.status,
        can_manage=manager,
        downloads_allowed=course.downloads_allowed,
        modules=curriculum_tree(db, course, manager, user),
    )


def _check_category(db: Session, category_id: uuid.UUID) -> None:
    cat = db.get(Category, category_id)
    if not cat or not cat.is_active:
        raise bad_request("Choose an active category")


@router.post("/courses", response_model=CourseDetail, status_code=201)
def create_course(
    body: CourseIn, request: Request, db: Session = Depends(get_db), user: User = Depends(require_author)
):
    _check_category(db, body.category_id)
    data = body.model_dump()
    price = data.pop("price_inr")
    course = Course(
        **data,
        owner_id=user.id,
        price_paise=round(price * 100),
        slug=unique_slug(db, Course, body.title),
        status=CourseStatus.DRAFT,
    )
    course.title = course.title.strip()
    db.add(course)
    db.flush()
    db.add(
        CoursePublicationReview(
            course_id=course.id, actor_id=user.id, action="create", from_status=None, to_status=CourseStatus.DRAFT
        )
    )
    audit(db, user.id, "course.created", "course", course.id, {"title": course.title}, client_ip(request))
    db.commit()
    return course_detail(str(course.id), request, db, user)


PRICE_LOCKED = {CourseStatus.APPROVED, CourseStatus.PUBLISHED, CourseStatus.UNPUBLISHED}


@router.patch("/courses/{course_id}", response_model=CourseDetail)
def update_course(
    course_id: uuid.UUID,
    body: CourseUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_author),
):
    course = get_course(db, course_id, for_update=True)
    require_edit(user, course)
    data = body.model_dump(exclude_unset=True)
    if "is_featured" in data and not user.is_admin:
        raise forbidden("Only an Admin can feature courses")
    if "price_inr" in data:
        new_price = round((data.pop("price_inr") or 0) * 100)
        if new_price != course.price_paise:
            if course.status in PRICE_LOCKED and not user.is_admin:
                raise AppError(409, "price_locked", "Price changes on approved or published courses need an Admin")
            data["price_paise"] = new_price
    if "category_id" in data and data["category_id"]:
        _check_category(db, data["category_id"])
    if "thumbnail_asset_id" in data and data["thumbnail_asset_id"]:
        asset = db.get(MediaAsset, data["thumbnail_asset_id"])
        if (
            not asset
            or asset.kind != MediaKind.IMAGE
            or asset.status != MediaStatus.READY
            or (asset.course_id not in (None, course.id))
            or (asset.owner_id != user.id and not user.is_admin)
        ):
            raise bad_request("Invalid thumbnail image")
    for k, v in data.items():
        if k in ("title", "description", "language") and v is None:
            continue
        setattr(course, k, v)
    audit(
        db,
        user.id,
        "course.updated",
        "course",
        course.id,
        {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in data.items() if k != "description"},
        client_ip(request),
    )
    db.commit()
    return course_detail(str(course.id), request, db, user)


@router.delete("/courses/{course_id}", response_model=Message)
def delete_course(
    course_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(require_author)
):
    course = get_course(db, course_id, for_update=True)
    require_manage(user, course)
    if not user.is_admin and course.status not in (CourseStatus.DRAFT, CourseStatus.CHANGES_REQUESTED):
        raise conflict("Only draft courses can be deleted. Ask an Admin to archive it instead.")
    has_buyers = (
        db.scalar(select(func.count()).select_from(PaymentRecord).where(PaymentRecord.course_id == course.id)) or 0
    )
    has_buyers += db.scalar(select(func.count()).select_from(Enrollment).where(Enrollment.course_id == course.id)) or 0
    if has_buyers:
        raise conflict("This course has payments or enrollments. Archive it instead of deleting.")
    course.deleted_at = datetime.now(UTC)
    course.slug = f"{course.slug}-deleted-{uuid.uuid4().hex[:6]}"
    audit(db, user.id, "course.deleted", "course", course.id, {"title": course.title}, client_ip(request))
    db.commit()
    return Message(message="Course deleted")


@router.get("/courses/{course_id}/reviews", response_model=list[PublicationReviewOut])
def review_history(course_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_author)):
    course = get_course(db, course_id)
    require_manage(user, course)
    rows = (
        db.scalars(
            select(CoursePublicationReview)
            .where(CoursePublicationReview.course_id == course.id)
            .order_by(CoursePublicationReview.created_at.desc())
        )
        .unique()
        .all()
    )
    return [
        PublicationReviewOut(
            id=r.id,
            action=r.action,
            from_status=r.from_status,
            to_status=r.to_status,
            comment=r.comment,
            actor_name=r.actor.full_name if r.actor else None,
            created_at=r.created_at,
        )
        for r in rows
    ]


def _do(action: str, course_id: uuid.UUID, body: ReviewActionIn, request: Request, db: Session, user: User):
    course = get_course(db, course_id, for_update=True)
    require_manage(user, course)
    lifecycle.transition(db, course, action, user, body.comment, body.override, client_ip(request))
    db.commit()
    return course_detail(str(course.id), request, db, user)


@router.post("/courses/{course_id}/submit-review", response_model=CourseDetail)
def submit_review(
    course_id: uuid.UUID,
    body: ReviewActionIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_author),
):
    return _do("submit", course_id, body, request, db, user)


@router.post("/courses/{course_id}/withdraw-review", response_model=CourseDetail)
def withdraw_review(
    course_id: uuid.UUID,
    body: ReviewActionIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_author),
):
    return _do("withdraw", course_id, body, request, db, user)


def _admin_action(action: str):
    def endpoint(
        course_id: uuid.UUID,
        body: ReviewActionIn,
        request: Request,
        db: Session = Depends(get_db),
        admin: User = Depends(require_admin),
    ):
        return _do(action, course_id, body, request, db, admin)

    endpoint.__name__ = f"admin_{action}_course"
    return endpoint


for _action, _path in [
    ("approve", "approve"),
    ("request_changes", "request-changes"),
    ("reject", "reject"),
    ("publish", "publish"),
    ("unpublish", "unpublish"),
    ("archive", "archive"),
    ("restore", "restore"),
]:
    router.add_api_route(
        f"/admin/courses/{{course_id}}/{_path}",
        _admin_action(_action),
        methods=["POST"],
        response_model=CourseDetail,
        tags=["admin"],
    )
