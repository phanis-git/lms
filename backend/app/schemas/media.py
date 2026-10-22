import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import MediaKind, MediaStatus


class UploadInitiateIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    kind: MediaKind
    course_id: uuid.UUID | None = None
    purpose: str = Field(default="lesson", pattern="^(lesson|resource|thumbnail)$")


class UploadInitiateOut(BaseModel):
    asset_id: uuid.UUID
    part_size: int
    part_urls: list[str]
    expires_in: int


class PartIn(BaseModel):
    part_number: int = Field(ge=1, le=10000)
    etag: str = Field(min_length=1, max_length=200)


class UploadCompleteIn(BaseModel):
    asset_id: uuid.UUID
    parts: list[PartIn] = Field(min_length=1, max_length=10000)


class AssetIdIn(BaseModel):
    asset_id: uuid.UUID


class MediaAssetOut(BaseModel):
    id: uuid.UUID
    kind: MediaKind
    status: MediaStatus
    original_filename: str
    content_type: str
    size_bytes: int
    duration_seconds: float | None
    created_at: datetime


class PlaybackOut(BaseModel):
    url: str
    expires_in: int
    content_type: str
    duration_seconds: float | None
    thumbnail_url: str | None = None
