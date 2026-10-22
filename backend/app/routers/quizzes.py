import random
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import client_ip, get_current_user, require_author
from app.core.errors import AppError, bad_request, conflict, not_found
from app.models import Lesson, LessonProgress, Quiz, QuizAnswer, QuizAttempt, QuizOption, QuizQuestion, User
from app.models.enums import LessonType, QuizReviewMode, QuizStatus, QuizUnlockRule
from app.schemas.common import Message
from app.schemas.learning import (
    AnswerReviewOut,
    AttemptIn,
    AttemptOut,
    DeliveryOptionOut,
    DeliveryQuestionOut,
    ManageOptionOut,
    ManageQuestionOut,
    QuestionIn,
    QuestionStat,
    QuizAnalyticsOut,
    QuizDeliveryOut,
    QuizManageOut,
    QuizSettingsIn,
)
from app.services.access import (
    can_manage,
    get_course,
    get_lesson,
    require_edit,
    require_manage,
    require_view_content,
)
from app.services.audit import audit
from app.services.progress import get_or_create_progress, mark_complete

router = APIRouter(tags=["quizzes"])


def _quiz(db: Session, quiz_id: uuid.UUID, lock: bool = False) -> Quiz:
    stmt = select(Quiz).where(Quiz.id == quiz_id)
    if lock:
        stmt = stmt.with_for_update(of=Quiz)
    q = db.scalar(stmt)
    if not q:
        raise not_found("Quiz")
    return q


def _managed_quiz(db: Session, user: User, quiz_id: uuid.UUID, edit: bool = False) -> Quiz:
    q = _quiz(db, quiz_id)
    course = get_course(db, q.course_id)
    (require_edit if edit else require_manage)(user, course)
    return q


def _manage_out(q: Quiz) -> QuizManageOut:
    return QuizManageOut(
        id=q.id,
        lesson_id=q.lesson_id,
        course_id=q.course_id,
        title=q.title,
        instructions=q.instructions,
        pass_percentage=q.pass_percentage,
        max_attempts=q.max_attempts,
        shuffle_questions=q.shuffle_questions,
        shuffle_options=q.shuffle_options,
        review_mode=q.review_mode,
        unlock_rule=q.unlock_rule,
        status=q.status,
        questions=[
            ManageQuestionOut(
                id=qq.id,
                text=qq.text,
                explanation=qq.explanation,
                difficulty=qq.difficulty,
                position=qq.position,
                options=[ManageOptionOut(id=o.id, text=o.text, is_correct=o.is_correct) for o in qq.options],
            )
            for qq in q.questions
        ],
    )


# ---------- Authoring ----------
@router.get("/quizzes/{quiz_id}/manage", response_model=QuizManageOut)
def manage_quiz(quiz_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_author)):
    return _manage_out(_managed_quiz(db, user, quiz_id))


@router.get("/lessons/{lesson_id}/quiz/manage", response_model=QuizManageOut)
def manage_quiz_by_lesson(lesson_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_author)):
    q = db.scalar(select(Quiz).where(Quiz.lesson_id == lesson_id))
    if not q:
        raise not_found("Quiz")
    return _manage_out(_managed_quiz(db, user, q.id))


@router.patch("/quizzes/{quiz_id}", response_model=QuizManageOut)
def update_quiz(
    quiz_id: uuid.UUID, body: QuizSettingsIn, db: Session = Depends(get_db), user: User = Depends(require_author)
):
    q = _managed_quiz(db, user, quiz_id, edit=True)
    data = body.model_dump(exclude_unset=True)
    if "max_attempts" in data and data["max_attempts"] == 0:
        data["max_attempts"] = None  # 0 = unlimited
    for k, v in data.items():
        setattr(q, k, v)
    db.commit()
    return _manage_out(q)


def _apply_question(qq: QuizQuestion, body: QuestionIn) -> None:
    qq.text, qq.explanation, qq.difficulty = body.text, body.explanation, body.difficulty
    qq.options.clear()
    for i, o in enumerate(body.options):
        qq.options.append(QuizOption(text=o.text, is_correct=o.is_correct, position=i))


def _has_attempts(db: Session, quiz_id: uuid.UUID) -> bool:
    return bool(db.scalar(select(func.count()).select_from(QuizAttempt).where(QuizAttempt.quiz_id == quiz_id)))


@router.post("/quizzes/{quiz_id}/questions", response_model=QuizManageOut, status_code=201)
def add_question(
    quiz_id: uuid.UUID, body: QuestionIn, db: Session = Depends(get_db), user: User = Depends(require_author)
):
    q = _managed_quiz(db, user, quiz_id, edit=True)
    qq = QuizQuestion(position=(max((x.position for x in q.questions), default=-1) + 1))
    _apply_question(qq, body)
    q.questions.append(qq)
    db.commit()
    db.refresh(q)
    return _manage_out(q)


def _managed_question(db: Session, user: User, question_id: uuid.UUID) -> QuizQuestion:
    qq = db.get(QuizQuestion, question_id)
    if not qq:
        raise not_found("Question")
    _managed_quiz(db, user, qq.quiz_id, edit=True)
    return qq


@router.put("/questions/{question_id}", response_model=QuizManageOut)
def update_question(
    question_id: uuid.UUID, body: QuestionIn, db: Session = Depends(get_db), user: User = Depends(require_author)
):
    qq = _managed_question(db, user, question_id)
    if _has_attempts(db, qq.quiz_id):
        # Keep existing answers meaningful: only text edits, same correct option position.
        old_correct = next(i for i, o in enumerate(qq.options) if o.is_correct)
        new_correct = next(i for i, o in enumerate(body.options) if o.is_correct)
        if len(body.options) != len(qq.options) or old_correct != new_correct:
            raise conflict(
                "Students have attempted this quiz; you can edit wording but not change options "
                "or the correct answer. Add a new question instead.",
                "quiz_has_attempts",
            )
        qq.text, qq.explanation, qq.difficulty = body.text, body.explanation, body.difficulty
        for o, new in zip(qq.options, body.options, strict=True):
            o.text = new.text
    else:
        _apply_question(qq, body)
    db.commit()
    return _manage_out(_quiz(db, qq.quiz_id))


@router.delete("/questions/{question_id}", response_model=QuizManageOut)
def delete_question(question_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_author)):
    qq = _managed_question(db, user, question_id)
    quiz_id = qq.quiz_id
    if _has_attempts(db, quiz_id):
        raise conflict("Students have attempted this quiz, so questions cannot be deleted", "quiz_has_attempts")
    db.delete(qq)
    db.commit()
    return _manage_out(_quiz(db, quiz_id))


@router.post("/quizzes/{quiz_id}/reorder", response_model=QuizManageOut)
def reorder_questions(
    quiz_id: uuid.UUID, ids: list[uuid.UUID], db: Session = Depends(get_db), user: User = Depends(require_author)
):
    q = _managed_quiz(db, user, quiz_id, edit=True)
    by_id = {qq.id: qq for qq in q.questions}
    if set(ids) != set(by_id):
        raise bad_request("Provide every question id exactly once")
    for i, qid in enumerate(ids):
        by_id[qid].position = i
    db.commit()
    db.refresh(q)
    return _manage_out(q)


@router.get("/quizzes/{quiz_id}/analytics", response_model=QuizAnalyticsOut)
def quiz_analytics(quiz_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_author)):
    q = _managed_quiz(db, user, quiz_id)
    attempts, students, passed, avg = db.execute(
        select(
            func.count(QuizAttempt.id),
            func.count(func.distinct(QuizAttempt.student_id)),
            func.count(QuizAttempt.id).filter(QuizAttempt.passed.is_(True)),
            func.coalesce(func.avg(QuizAttempt.percentage), 0),
        ).where(QuizAttempt.quiz_id == q.id)
    ).one()
    stats = {
        qid: (n, c)
        for qid, n, c in db.execute(
            select(
                QuizAnswer.question_id,
                func.count(QuizAnswer.id),
                func.count(QuizAnswer.id).filter(QuizAnswer.is_correct.is_(True)),
            )
            .join(QuizAttempt, QuizAttempt.id == QuizAnswer.attempt_id)
            .where(QuizAttempt.quiz_id == q.id)
            .group_by(QuizAnswer.question_id)
        )
    }
    return QuizAnalyticsOut(
        quiz_id=q.id,
        title=q.title,
        attempts=attempts,
        students=students,
        pass_rate=round(passed * 100 / attempts, 1) if attempts else 0,
        average_percentage=round(float(avg), 1),
        questions=[
            QuestionStat(
                question_id=qq.id,
                text=qq.text,
                answered=stats.get(qq.id, (0, 0))[0],
                correct=stats.get(qq.id, (0, 0))[1],
                correct_rate=round(stats[qq.id][1] * 100 / stats[qq.id][0], 1) if stats.get(qq.id, (0, 0))[0] else 0,
            )
            for qq in q.questions
        ],
    )


# ---------- Student delivery & grading ----------
def _lock_reason(db: Session, user: User, les: Lesson, q: Quiz) -> str | None:
    if q.unlock_rule != QuizUnlockRule.AFTER_FOLDER_LESSONS:
        return None
    siblings = db.scalars(
        select(Lesson.id).where(
            Lesson.course_id == les.course_id,
            Lesson.module_id == les.module_id,
            Lesson.folder_id.is_(None) if les.folder_id is None else Lesson.folder_id == les.folder_id,
            Lesson.archived_at.is_(None),
            Lesson.is_required.is_(True),
            Lesson.lesson_type != LessonType.QUIZ,
        )
    ).all()
    if not siblings:
        return None
    done = db.scalar(
        select(func.count())
        .select_from(LessonProgress)
        .where(
            LessonProgress.student_id == user.id,
            LessonProgress.lesson_id.in_(siblings),
            LessonProgress.completed.is_(True),
        )
    )
    if done < len(siblings):
        return f"Complete the {len(siblings) - done} remaining lesson(s) in this section to unlock the quiz"
    return None


def _student_quiz(db: Session, user: User, lesson: Lesson) -> Quiz:
    course = get_course(db, lesson.course_id)
    require_view_content(db, user, course)
    q = db.scalar(select(Quiz).where(Quiz.lesson_id == lesson.id))
    if not q or (q.status != QuizStatus.PUBLISHED and not can_manage(user, course)):
        raise not_found("Quiz")
    return q


@router.get("/lessons/{lesson_id}/quiz", response_model=QuizDeliveryOut)
def deliver_quiz(lesson_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    les = get_lesson(db, lesson_id)
    q = _student_quiz(db, user, les)
    used, best, any_pass = db.execute(
        select(func.count(QuizAttempt.id), func.max(QuizAttempt.percentage), func.bool_or(QuizAttempt.passed)).where(
            QuizAttempt.quiz_id == q.id, QuizAttempt.student_id == user.id
        )
    ).one()
    reason = _lock_reason(db, user, les, q)
    rng = random.Random(f"{user.id}:{q.id}:{used}")  # noqa: S311 - shuffling, not security
    questions = list(q.questions)
    if q.shuffle_questions:
        rng.shuffle(questions)
    out = []
    for qq in questions:
        opts = list(qq.options)
        if q.shuffle_options:
            rng.shuffle(opts)
        # Correct answers and explanations are never part of the delivery payload.
        out.append(
            DeliveryQuestionOut(
                id=qq.id,
                text=qq.text,
                difficulty=qq.difficulty,
                options=[DeliveryOptionOut(id=o.id, text=o.text) for o in opts],
            )
        )
    return QuizDeliveryOut(
        id=q.id,
        lesson_id=les.id,
        title=q.title,
        instructions=q.instructions,
        pass_percentage=q.pass_percentage,
        max_attempts=q.max_attempts,
        attempts_used=used,
        best_percentage=best,
        passed=bool(any_pass),
        locked=reason is not None,
        locked_reason=reason,
        questions=[] if reason else out,
    )


def _attempt_out(q: Quiz, a: QuizAttempt) -> AttemptOut:
    review = []
    if q.review_mode != QuizReviewMode.SCORE_ONLY:
        qs = {qq.id: qq for qq in q.questions}
        for ans in a.answers:
            qq = qs.get(ans.question_id)
            if not qq:
                continue
            full = q.review_mode == QuizReviewMode.FULL
            correct = next((o.id for o in qq.options if o.is_correct), None)
            review.append(
                AnswerReviewOut(
                    question_id=qq.id,
                    question_text=qq.text,
                    selected_option_id=ans.selected_option_id,
                    is_correct=ans.is_correct,
                    correct_option_id=correct if full else None,
                    explanation=qq.explanation if full else None,
                )
            )
    return AttemptOut(
        id=a.id,
        quiz_id=a.quiz_id,
        attempt_number=a.attempt_number,
        score=a.score,
        total=a.total,
        percentage=a.percentage,
        passed=a.passed,
        submitted_at=a.submitted_at,
        review=review,
    )


@router.post("/quizzes/{quiz_id}/attempts", response_model=AttemptOut, status_code=201)
def submit_attempt(
    quiz_id: uuid.UUID,
    body: AttemptIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = _quiz(db, quiz_id)
    les = get_lesson(db, q.lesson_id)
    q = _student_quiz(db, user, les)
    course = get_course(db, q.course_id)
    if can_manage(user, course):
        raise bad_request("Course authors can preview but not submit quizzes")
    if reason := _lock_reason(db, user, les, q):
        raise AppError(409, "quiz_locked", reason)
    # Serialise attempts per student so the attempt limit cannot be raced.
    db.execute(select(User.id).where(User.id == user.id).with_for_update())
    used = db.scalar(
        select(func.count())
        .select_from(QuizAttempt)
        .where(QuizAttempt.quiz_id == q.id, QuizAttempt.student_id == user.id)
    )
    if q.max_attempts and used >= q.max_attempts:
        raise AppError(409, "attempts_exhausted", "You have used all attempts for this quiz")
    chosen = {a.question_id: a.option_id for a in body.answers}
    unknown = set(chosen) - {qq.id for qq in q.questions}
    if unknown:
        raise bad_request("Answers include questions that are not part of this quiz")
    score = 0
    attempt = QuizAttempt(
        quiz_id=q.id,
        student_id=user.id,
        attempt_number=used + 1,
        score=0,
        total=len(q.questions),
        percentage=0,
        passed=False,
        submitted_at=datetime.now(UTC),
    )
    for qq in q.questions:
        sel = chosen.get(qq.id)
        if sel is not None and sel not in {o.id for o in qq.options}:
            raise bad_request("An answer refers to an option from a different question")
        ok = sel is not None and any(o.id == sel and o.is_correct for o in qq.options)
        score += ok
        attempt.answers.append(QuizAnswer(question_id=qq.id, selected_option_id=sel, is_correct=ok))
    attempt.score = score
    attempt.percentage = round(score * 100 / len(q.questions), 1) if q.questions else 0
    attempt.passed = attempt.percentage >= q.pass_percentage
    db.add(attempt)
    db.flush()
    if attempt.passed:
        p = get_or_create_progress(db, user.id, les)
        p.last_activity_at = datetime.now(UTC)
        mark_complete(db, p)
    audit(
        db,
        user.id,
        "quiz.attempted",
        "quiz",
        q.id,
        {"attempt": attempt.attempt_number, "percentage": attempt.percentage},
        client_ip(request),
    )
    db.commit()
    db.refresh(attempt)
    return _attempt_out(q, attempt)


@router.get("/quizzes/{quiz_id}/attempts", response_model=list[AttemptOut])
def my_attempts(quiz_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = _quiz(db, quiz_id)
    rows = db.scalars(
        select(QuizAttempt)
        .where(QuizAttempt.quiz_id == q.id, QuizAttempt.student_id == user.id)
        .order_by(QuizAttempt.attempt_number.desc())
    ).all()
    return [_attempt_out(q, a) for a in rows]


@router.delete("/quizzes/{quiz_id}/attempts/{student_id}", response_model=Message)
def reset_attempts(
    quiz_id: uuid.UUID,
    student_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_author),
):
    q = _managed_quiz(db, user, quiz_id)
    for a in db.scalars(
        select(QuizAttempt).where(QuizAttempt.quiz_id == q.id, QuizAttempt.student_id == student_id)
    ).all():
        db.delete(a)
    audit(db, user.id, "quiz.attempts_reset", "quiz", q.id, {"student_id": str(student_id)}, client_ip(request))
    db.commit()
    return Message(message="Attempts reset")


# Declared last so concrete routes such as /attempts take precedence over the {action} catch-all.
@router.post("/quizzes/{quiz_id}/{action}", response_model=QuizManageOut)
def quiz_status(
    quiz_id: uuid.UUID,
    action: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_author),
):
    target = {"publish": QuizStatus.PUBLISHED, "unpublish": QuizStatus.DRAFT, "archive": QuizStatus.ARCHIVED}.get(
        action
    )
    if not target:
        raise not_found("Action")
    q = _managed_quiz(db, user, quiz_id, edit=True)
    if target == QuizStatus.PUBLISHED:
        if not q.questions:
            raise bad_request("Add at least one question before publishing")
        for qq in q.questions:
            if sum(o.is_correct for o in qq.options) != 1:
                raise bad_request(f"Question {qq.position + 1} needs exactly one correct option")
    q.status = target
    audit(db, user.id, f"quiz.{action}", "quiz", q.id, None, client_ip(request))
    db.commit()
    return _manage_out(q)
