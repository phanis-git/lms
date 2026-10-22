import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.enums import CourseStatus, LessonType, MediaStatus
from app.schemas.common import ORMModel


class CategoryIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    icon: str | None = Field(default=None, max_length=60)
    is_active: bool = True
    position: int = 0


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    icon: str | None = Field(default=None, max_length=60)
    is_active: bool | None = None
    position: int | None = None


class CategoryOut(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    icon: str | None
    is_active: bool
    position: int
    course_count: int = 0


def _clean_list(v: list[str]) -> list[str]:
    return [s.strip() for s in v if s and s.strip()][:30]


class CourseIn(BaseModel):
    title: str = Field(min_length=3, max_length=160)
    category_id: uuid.UUID
    subtitle: str | None = Field(default=None, max_length=240)
    description: str = Field(default="", max_length=20000)
    learning_outcomes: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    level: str | None = Field(default=None, max_length=40)
    language: str = Field(default="English", max_length=40)
    price_inr: float = Field(default=0, ge=0, le=500000)
    curriculum_public: bool = True
    downloads_allowed: bool = True

    _lists = field_validator("learning_outcomes", "prerequisites")(_clean_list)


class CourseUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=160)
    category_id: uuid.UUID | None = None
    subtitle: str | None = Field(default=None, max_length=240)
    description: str | None = Field(default=None, max_length=20000)
    learning_outcomes: list[str] | None = None
    prerequisites: list[str] | None = None
    level: str | None = Field(default=None, max_length=40)
    language: str | None = Field(default=None, max_length=40)
    price_inr: float | None = Field(default=None, ge=0, le=500000)
    curriculum_public: bool | None = None
    downloads_allowed: bool | None = None
    is_featured: bool | None = None
    thumbnail_asset_id: uuid.UUID | None = None

    @field_validator("learning_outcomes", "prerequisites")
    @classmethod
    def _lists(cls, v):
        return _clean_list(v) if v is not None else v


class PersonOut(BaseModel):
    id: uuid.UUID
    full_name: str
    headline: str | None = None
    bio: str | None = None


class CourseSummary(BaseModel):
    id: uuid.UUID
    slug: str
    title: str
    subtitle: str | None
    description: str
    category: CategoryOut
    instructor: PersonOut
    price_inr: float
    status: CourseStatus
    level: str | None
    language: str
    thumbnail_url: str | None
    is_featured: bool
    module_count: int = 0
    lesson_count: int = 0
    total_duration_seconds: float = 0
    published_at: datetime | None
    updated_at: datetime
    enrollment_status: str | None = None
    progress_percent: int | None = None


class OutlineLesson(BaseModel):
    id: uuid.UUID
    title: str
    lesson_type: LessonType
    duration_seconds: float | None = None
    is_preview: bool = False
    is_required: bool = True


class OutlineFolder(BaseModel):
    id: uuid.UUID
    title: str
    folders: list["OutlineFolder"] = Field(default_factory=list)
    lessons: list[OutlineLesson] = Field(default_factory=list)


class OutlineModule(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None = None
    folders: list[OutlineFolder] = Field(default_factory=list)
    lessons: list[OutlineLesson] = Field(default_factory=list)


class CourseDetail(CourseSummary):
    learning_outcomes: list[str]
    prerequisites: list[str]
    curriculum_public: bool
    downloads_allowed: bool
    can_manage: bool = False
    can_access_content: bool = False
    is_purchasable: bool = False
    outline: list[OutlineModule] | None = None
    thumbnail_asset_id: uuid.UUID | None = None
    pending_payment: bool = False


class ReviewActionIn(BaseModel):
    comment: str | None = Field(default=None, max_length=5000)
    override: bool = False


class PublicationReviewOut(BaseModel):
    id: uuid.UUID
    action: str
    from_status: CourseStatus | None
    to_status: CourseStatus | None
    comment: str | None
    actor_name: str | None
    created_at: datetime


# ---- Curriculum management ----
class ModuleIn(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)


class ModuleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)


class FolderIn(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    parent_id: uuid.UUID | None = None


class FolderUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    parent_id: uuid.UUID | None = None
    module_id: uuid.UUID | None = None
    move: bool = False


class LessonIn(BaseModel):
    module_id: uuid.UUID
    folder_id: uuid.UUID | None = None
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    lesson_type: LessonType
    content: str | None = Field(default=None, max_length=100000)
    is_required: bool = True
    is_preview: bool = False


class LessonUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    content: str | None = Field(default=None, max_length=100000)
    is_required: bool | None = None
    is_preview: bool | None = None
    video_asset_id: uuid.UUID | None = None
    clear_video: bool = False
    module_id: uuid.UUID | None = None
    folder_id: uuid.UUID | None = None
    move: bool = False


class ReorderIn(BaseModel):
    kind: str = Field(pattern="^(module|folder|lesson)$")
    ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)


class ResourceIn(BaseModel):
    media_asset_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)
    downloadable: bool = True


class ResourceUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    downloadable: bool | None = None


class ResourceOut(BaseModel):
    id: uuid.UUID
    title: str
    downloadable: bool
    filename: str
    content_type: str
    size_bytes: int


class VideoInfo(BaseModel):
    asset_id: uuid.UUID
    filename: str
    status: MediaStatus
    size_bytes: int
    duration_seconds: float | None


class TreeLesson(BaseModel):
    id: uuid.UUID
    module_id: uuid.UUID
    folder_id: uuid.UUID | None
    title: str
    description: str | None
    lesson_type: LessonType
    content: str | None = None
    position: int
    is_required: bool
    is_preview: bool
    video: VideoInfo | None = None
    resources: list[ResourceOut] = Field(default_factory=list)
    quiz_id: uuid.UUID | None = None
    quiz_status: str | None = None
    completed: bool = False
    last_position_seconds: float = 0
    locked: bool = False


class TreeFolder(BaseModel):
    id: uuid.UUID
    module_id: uuid.UUID
    parent_id: uuid.UUID | None
    title: str
    position: int
    folders: list["TreeFolder"] = Field(default_factory=list)
    lessons: list[TreeLesson] = Field(default_factory=list)


class TreeModule(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    position: int
    folders: list[TreeFolder] = Field(default_factory=list)
    lessons: list[TreeLesson] = Field(default_factory=list)


class CurriculumOut(BaseModel):
    course_id: uuid.UUID
    course_title: str
    course_status: CourseStatus
    can_manage: bool
    downloads_allowed: bool
    modules: list[TreeModule]


class ModuleOut(ORMModel):
    id: uuid.UUID
    course_id: uuid.UUID
    title: str
    description: str | None
    position: int


class FolderOut(ORMModel):
    id: uuid.UUID
    course_id: uuid.UUID
    module_id: uuid.UUID
    parent_id: uuid.UUID | None
    title: str
    position: int


class LessonOut(ORMModel):
    id: uuid.UUID
    course_id: uuid.UUID
    module_id: uuid.UUID
    folder_id: uuid.UUID | None
    title: str
    description: str | None
    lesson_type: LessonType
    content: str | None
    video_asset_id: uuid.UUID | None
    position: int
    is_required: bool
    is_preview: bool
