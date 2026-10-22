from fastapi import Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session


class PageParams(BaseModel):
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def page_params(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> PageParams:
    return PageParams(page=page, page_size=page_size)


def paginate(db: Session, stmt: Select, params: PageParams, scalars: bool = True):
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    q = stmt.offset(params.offset).limit(params.page_size)
    rows = db.scalars(q).unique().all() if scalars else db.execute(q).all()
    return rows, total
