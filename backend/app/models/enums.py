import enum


class Role(str, enum.Enum):
    ADMIN = "ADMIN"
    INSTRUCTOR = "INSTRUCTOR"
    STUDENT = "STUDENT"


class UserStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class ApprovalStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class CourseStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    SUBMITTED_FOR_REVIEW = "SUBMITTED_FOR_REVIEW"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    APPROVED = "APPROVED"
    PUBLISHED = "PUBLISHED"
    UNPUBLISHED = "UNPUBLISHED"
    ARCHIVED = "ARCHIVED"


class LessonType(str, enum.Enum):
    VIDEO = "VIDEO"
    TEXT = "TEXT"
    FILE = "FILE"
    QUIZ = "QUIZ"


class MediaKind(str, enum.Enum):
    VIDEO = "VIDEO"
    DOCUMENT = "DOCUMENT"
    IMAGE = "IMAGE"


class MediaStatus(str, enum.Enum):
    PENDING_UPLOAD = "PENDING_UPLOAD"
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"
    DELETED = "DELETED"


class PaymentMethod(str, enum.Enum):
    MANUAL_UPI = "MANUAL_UPI"
    GATEWAY = "GATEWAY"


class PaymentStatus(str, enum.Enum):
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REFUNDED = "REFUNDED"
    # Gateway flow states
    CREATED = "CREATED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class EnrollmentStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class EnrollmentSource(str, enum.Enum):
    PAYMENT = "PAYMENT"
    ADMIN_GRANT = "ADMIN_GRANT"


class QuizStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"


class QuizReviewMode(str, enum.Enum):
    SCORE_ONLY = "SCORE_ONLY"
    CORRECTNESS = "CORRECTNESS"
    FULL = "FULL"


class QuizUnlockRule(str, enum.Enum):
    ALWAYS = "ALWAYS"
    AFTER_FOLDER_LESSONS = "AFTER_FOLDER_LESSONS"


class Difficulty(str, enum.Enum):
    EASY = "EASY"
    MEDIUM = "MEDIUM"
    HARD = "HARD"


class EmailStatus(str, enum.Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
