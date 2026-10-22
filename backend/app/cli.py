"""Operational commands.

python -m app.cli create-admin --email you@example.com --name "Your Name"   (prompts for password)
python -m app.cli seed-demo        (development only: categories + a sample draft course)
python -m app.cli deliver-emails
"""

import argparse
import getpass
import os
import sys

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models import Category, User, UserProfile
from app.models.enums import Role, UserStatus
from app.services.audit import audit
from app.services.catalog import slugify


def create_admin(email: str, name: str, password: str | None) -> int:
    email = email.strip().lower()
    password = password or os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
    if len(password) < 12:
        print("Password must be at least 12 characters", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        from datetime import UTC, datetime

        user = db.scalar(select(User).where(func.lower(User.email) == email))
        if user:
            user.role = Role.ADMIN
            user.status = UserStatus.ACTIVE
            user.email_verified_at = user.email_verified_at or datetime.now(UTC)
            action = "promoted"
        else:
            user = User(
                email=email,
                full_name=name,
                password_hash=hash_password(password),
                role=Role.ADMIN,
                status=UserStatus.ACTIVE,
                email_verified_at=datetime.now(UTC),
            )
            user.profile = UserProfile()
            db.add(user)
            action = "created"
        db.flush()
        audit(db, None, "admin.bootstrap", "user", user.id, {"email": email, "action": action})
        db.commit()
    print(f"Admin {action}: {email}")
    return 0


def seed_demo() -> int:
    if get_settings().environment == "production":
        print("Refusing to seed demo data in production", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        for i, name in enumerate(["AWS DevOps", "Java Full Stack", "SQL", "Python Full Stack"]):
            if not db.scalar(select(Category).where(Category.slug == slugify(name))):
                db.add(Category(name=name, slug=slugify(name), position=i, is_active=True))
        db.commit()
    print("Seeded categories")
    return 0


def deliver_emails() -> int:
    from app.services.notify import deliver_pending_emails

    with SessionLocal() as db:
        print(f"Delivered {deliver_pending_emails(db, limit=200)} email(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("create-admin")
    a.add_argument("--email", required=True)
    a.add_argument("--name", default="Platform Admin")
    a.add_argument("--password-stdin", action="store_true", help="read password from stdin")
    sub.add_parser("seed-demo")
    sub.add_parser("deliver-emails")
    args = p.parse_args(argv)
    if args.cmd == "create-admin":
        pw = sys.stdin.readline().rstrip("\n") if args.password_stdin else None
        return create_admin(args.email, args.name, pw)
    if args.cmd == "seed-demo":
        return seed_demo()
    return deliver_emails()


if __name__ == "__main__":
    raise SystemExit(main())
