"""In-app notifications and the transactional email outbox."""

import html
import logging
import smtplib
import ssl
import uuid
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import EmailOutbox, Notification, User
from app.models.base import utcnow
from app.models.enums import EmailStatus, Role, UserStatus

log = logging.getLogger(__name__)


def notify(
    db: Session, user_id: uuid.UUID, type_: str, title: str, body: str | None = None, link: str | None = None
) -> None:
    db.add(Notification(user_id=user_id, type=type_, title=title, body=body, link=link))


def notify_admins(db: Session, type_: str, title: str, body: str | None = None, link: str | None = None):
    admin_ids = db.scalars(select(User.id).where(User.role == Role.ADMIN, User.status == UserStatus.ACTIVE)).all()
    for admin_id in admin_ids:
        notify(db, admin_id, type_, title, body, link)


def queue_email(db: Session, to: str, subject: str, body_text: str, dedupe_key: str) -> None:
    """Queue an email; the dedupe key guarantees a logical email is sent at most once."""
    paragraphs = "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in body_text.split("\n\n"))
    body_html = (
        '<div style="font-family:Arial,sans-serif;max-width:560px;margin:auto;color:#0f172a">'
        '<h2 style="color:#0b1f3a">DevOps Freshers</h2>'
        f"{paragraphs}"
        '<p style="color:#64748b;font-size:12px">This is an automated message from DevOps Freshers.</p>'
        "</div>"
    )
    stmt = (
        insert(EmailOutbox)
        .values(
            id=uuid.uuid4(),
            to_address=to,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            dedupe_key=dedupe_key[:200],
            status=EmailStatus.PENDING,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
    )
    db.execute(stmt)


def _send_smtp(msg: EmailMessage) -> None:
    s = get_settings()
    if s.smtp_use_ssl:
        with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ssl.create_default_context(), timeout=20) as c:
            if s.smtp_username:
                c.login(s.smtp_username, s.smtp_password)
            c.send_message(msg)
        return
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as c:
        if s.smtp_use_tls:
            c.starttls(context=ssl.create_default_context())
        if s.smtp_username:
            c.login(s.smtp_username, s.smtp_password)
        c.send_message(msg)


def deliver_pending_emails(db: Session, limit: int = 20) -> int:
    """Send queued emails. Uses SKIP LOCKED so multiple workers never send the same email."""
    s = get_settings()
    rows = db.scalars(
        select(EmailOutbox)
        .where(EmailOutbox.status == EmailStatus.PENDING, EmailOutbox.attempts < 5)
        .order_by(EmailOutbox.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    sent = 0
    for row in rows:
        row.attempts += 1
        if not s.smtp_host:
            row.status = EmailStatus.SKIPPED
            row.last_error = "SMTP not configured"
            log.warning("SMTP not configured; email %s (%s) not sent", row.id, row.subject)
            continue
        msg = EmailMessage()
        msg["From"] = s.email_from
        msg["To"] = row.to_address
        msg["Subject"] = row.subject
        msg.set_content(row.body_text)
        if row.body_html:
            msg.add_alternative(row.body_html, subtype="html")
        try:
            _send_smtp(msg)
            row.status = EmailStatus.SENT
            row.sent_at = utcnow()
            sent += 1
        except Exception as exc:  # noqa: BLE001 - recorded and retried
            row.last_error = str(exc)[:500]
            if row.attempts >= 5:
                row.status = EmailStatus.FAILED
            log.warning("Email %s delivery failed: %s", row.id, type(exc).__name__)
    db.commit()
    return sent
