import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.models.enums import Difficulty, LessonType, QuizReviewMode, QuizStatus, QuizUnlockRule


class ProgressIn(BaseModel):
    position_seconds: float = Field(ge=0, le=86400)
    duration_seconds: float | None = Field(default=None, gt=0, le=86400)
    watched_delta_seconds: float = Field(default=0, ge=0, le=600)


class LessonProgressOut(BaseModel):
    lesson_id: uuid.UUID
    last_position_seconds: float
    duration_seconds: float | None
    watched_seconds: float
    completed: bool
    completed_at: datetime | None
    last_activity_at: datetime | None


class CourseProgressOut(BaseModel):
    course_id: uuid.UUID
    total_required: int
    completed_required: int
    percent: int
    completed: bool
    completed_at: datetime | None
    resume_lesson_id: uuid.UUID | None
    lessons: list[LessonProgressOut]


class OptionIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    is_correct: bool = False


class QuestionIn(BaseModel):
    text: str = Field(min_length=3, max_length=4000)
    explanation: str | None = Field(default=None, max_length=4000)
    difficulty: Difficulty | None = None
    options: list[OptionIn] = Field(min_length=2, max_length=8)

    @model_validator(mode="after")
    def _one_correct(self):
        if sum(o.is_correct for o in self.options) != 1:
            raise ValueError("Exactly one option must be marked correct")
        return self


class QuizSettingsIn(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=200)
    instructions: str | None = Field(default=None, max_length=4000)
    pass_percentage: int | None = Field(default=None, ge=1, le=100)
    max_attempts: int | None = Field(default=None, ge=0, le=100)
    shuffle_questions: bool | None = None
    shuffle_options: bool | None = None
    review_mode: QuizReviewMode | None = None
    unlock_rule: QuizUnlockRule | None = None


class ManageOptionOut(BaseModel):
    id: uuid.UUID
    text: str
    is_correct: bool


class ManageQuestionOut(BaseModel):
    id: uuid.UUID
    text: str
    explanation: str | None
    difficulty: Difficulty | None
    position: int
    options: list[ManageOptionOut]


class QuizManageOut(BaseModel):
    id: uuid.UUID
    lesson_id: uuid.UUID
    course_id: uuid.UUID
    title: str
    instructions: str | None
    pass_percentage: int
    max_attempts: int | None
    shuffle_questions: bool
    shuffle_options: bool
    review_mode: QuizReviewMode
    unlock_rule: QuizUnlockRule
    status: QuizStatus
    questions: list[ManageQuestionOut]


class DeliveryOptionOut(BaseModel):
    id: uuid.UUID
    text: str


class DeliveryQuestionOut(BaseModel):
    id: uuid.UUID
    text: str
    difficulty: Difficulty | None
    options: list[DeliveryOptionOut]


class QuizDeliveryOut(BaseModel):
    id: uuid.UUID
    lesson_id: uuid.UUID
    title: str
    instructions: str | None
    pass_percentage: int
    max_attempts: int | None
    attempts_used: int
    best_percentage: float | None
    passed: bool
    locked: bool
    locked_reason: str | None
    questions: list[DeliveryQuestionOut]


class AnswerIn(BaseModel):
    question_id: uuid.UUID
    option_id: uuid.UUID | None = None


class AttemptIn(BaseModel):
    answers: list[AnswerIn] = Field(max_length=500)


class AnswerReviewOut(BaseModel):
    question_id: uuid.UUID
    question_text: str
    selected_option_id: uuid.UUID | None
    is_correct: bool | None
    correct_option_id: uuid.UUID | None
    explanation: str | None


class AttemptOut(BaseModel):
    id: uuid.UUID
    quiz_id: uuid.UUID
    attempt_number: int
    score: int
    total: int
    percentage: float
    passed: bool
    submitted_at: datetime
    review: list[AnswerReviewOut] = []


class QuestionStat(BaseModel):
    question_id: uuid.UUID
    text: str
    answered: int
    correct: int
    correct_rate: float


class QuizAnalyticsOut(BaseModel):
    quiz_id: uuid.UUID
    title: str
    attempts: int
    students: int
    pass_rate: float
    average_percentage: float
    questions: list[QuestionStat]


class NotificationOut(BaseModel):
    id: uuid.UUID
    type: str
    title: str
    body: str | None
    link: str | None
    read_at: datetime | None
    created_at: datetime


class LessonNavOut(BaseModel):
    id: uuid.UUID
    title: str
    lesson_type: LessonType
