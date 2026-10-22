import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import client_ip, get_optional_user, require_admin
from app.core.errors import conflict, not_found
from app.models import Category, Course, User
from app.models.enums import CourseStatus, Role
from app.schemas.common import Message
from app.schemas.courses import CategoryIn, CategoryOut, CategoryUpdate
from app.services.audit import audit
from app.services.catalog import category_out, unique_slug

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("", response_model=list[CategoryOut])
def list_categories(
    include_inactive: bool = False, db: Session = Depends(get_db), user: User | None = Depends(get_optional_user)
):
    stmt = select(Category).order_by(Category.position, Category.name)
    if not (include_inactive and user and user.role == Role.ADMIN):
        stmt = stmt.where(Category.is_active.is_(True))
    counts = dict(
        db.execute(
            select(Course.category_id, func.count())
            .where(Course.status == CourseStatus.PUBLISHED, Course.deleted_at.is_(None))
            .group_by(Course.category_id)
        ).all()
    )
    return [category_out(c, counts.get(c.id, 0)) for c in db.scalars(stmt)]


def _name_taken(db: Session, name: str, exclude: uuid.UUID | None = None) -> bool:
    stmt = select(Category.id).where(func.lower(Category.name) == name.strip().lower())
    if exclude:
        stmt = stmt.where(Category.id != exclude)
    return db.scalar(stmt) is not None


@router.post("", response_model=CategoryOut, status_code=201)
def create_category(
    body: CategoryIn, request: Request, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    if _name_taken(db, body.name):
        raise conflict("A category with this name already exists")
    c = Category(**body.model_dump(), slug=unique_slug(db, Category, body.name))
    c.name = c.name.strip()
    db.add(c)
    db.flush()
    audit(db, admin.id, "category.created", "category", c.id, {"name": c.name}, client_ip(request))
    db.commit()
    return category_out(c)


@router.patch("/{category_id}", response_model=CategoryOut)
def update_category(
    category_id: uuid.UUID,
    body: CategoryUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    c = db.get(Category, category_id)
    if not c:
        raise not_found("Category")
    data = body.model_dump(exclude_unset=True)
    if "name" in data and data["name"] and _name_taken(db, data["name"], c.id):
        raise conflict("A category with this name already exists")
    for k, v in data.items():
        if v is not None or k in ("description", "icon"):
            setattr(c, k, v)
    audit(db, admin.id, "category.updated", "category", c.id, data, client_ip(request))
    db.commit()
    return category_out(c)


@router.delete("/{category_id}", response_model=Message)
def delete_category(
    category_id: uuid.UUID, request: Request, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    c = db.get(Category, category_id)
    if not c:
        raise not_found("Category")
    in_use = db.scalar(select(func.count()).select_from(Course).where(Course.category_id == c.id)) or 0
    if in_use:
        raise conflict(
            f"{in_use} course(s) use this category. Deactivate it instead, or move the courses first.",
            "category_in_use",
        )
    db.delete(c)
    audit(db, admin.id, "category.deleted", "category", category_id, {"name": c.name}, client_ip(request))
    db.commit()
    return Message(message="Category deleted")
