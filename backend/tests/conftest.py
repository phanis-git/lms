import os
import re
import uuid

os.environ.update(
    ENVIRONMENT="test",
    DATABASE_URL=os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://lms:lms@localhost:5432/lms_test"),
    COOKIE_SECURE="false",
    AUTH_RATE_LIMIT_PER_MINUTE="1000",
    SMTP_HOST="",
    S3_BUCKET="test-bucket",
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402


def _ensure_database(url: str) -> None:
    u = make_url(url)
    admin = create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        if not c.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": u.database}):
            c.execute(text(f'CREATE DATABASE "{u.database}"'))
    admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def _schema():
    from alembic.config import Config

    from alembic import command

    _ensure_database(os.environ["DATABASE_URL"])
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield


class FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, str] = {}

    def install(self, mp):
        from app.services import storage

        mp.setattr(storage, "start_multipart", self.start_multipart)
        mp.setattr(
            storage,
            "presign_parts",
            lambda key, uid, n, origin: [f"https://s3.test/{key}?part={i + 1}" for i in range(n)],
        )
        mp.setattr(storage, "complete_multipart", self.complete)
        mp.setattr(storage, "abort_multipart", lambda key, uid: self.uploads.pop(uid, None))
        mp.setattr(
            storage, "head", lambda key: {"ContentLength": len(self.objects[key])} if key in self.objects else None
        )
        mp.setattr(storage, "read_prefix", lambda key, nbytes=64: self.objects[key][:nbytes])
        mp.setattr(storage, "put_object", lambda key, body, ct: self.objects.__setitem__(key, body.read()))
        mp.setattr(storage, "delete_object", lambda key: self.objects.pop(key, None))
        mp.setattr(
            storage,
            "signed_get_url",
            lambda key, origin, filename=None, download=False, ttl=None: f"https://s3.test/{key}?sig=short-lived",
        )

    def start_multipart(self, key, content_type):
        uid = uuid.uuid4().hex
        self.uploads[uid] = key
        return uid

    def complete(self, key, uid, parts):
        self.uploads.pop(uid)
        self.objects.setdefault(key, b"")

    def simulate_upload(self, key: str, data: bytes):
        self.objects[key] = data


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    fs = FakeStorage()
    fs.install(monkeypatch)
    return fs


@pytest.fixture(autouse=True)
def _clean():
    from app.core.db import engine

    with engine.begin() as c:
        tables = c.scalars(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'alembic_version'")
        ).all()
        c.execute(text("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def db():
    from app.core.db import SessionLocal

    s = SessionLocal()
    yield s
    s.close()


PASSWORD = "Str0ngPassw0rd!"


def new_client() -> TestClient:
    from app.main import app

    return TestClient(app)


def latest_email(db, to: str):
    from app.models import EmailOutbox

    db.expire_all()
    return db.scalars(
        select(EmailOutbox).where(EmailOutbox.to_address == to).order_by(EmailOutbox.created_at.desc())
    ).first()


def login(email: str, password: str = PASSWORD) -> TestClient:
    c = new_client()
    r = c.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    c.headers["X-XSRF-TOKEN"] = r.json()["csrf_token"]
    c.user = r.json()["user"]
    return c


def register_verified(db, email: str, name: str = "Test User", role: str = "STUDENT") -> TestClient:
    c = new_client()
    r = c.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "full_name": name,
            "requested_role": role,
            "motivation": "I teach DevOps",
        },
    )
    assert r.status_code == 201, r.text
    token = re.search(r"token=([\w\-]+)", latest_email(db, email).body_text).group(1)
    assert c.post("/api/v1/auth/verify-email", json={"token": token}).status_code == 200
    return login(email)


@pytest.fixture
def admin(db):
    from app.cli import create_admin

    assert create_admin("admin@example.com", "Admin", PASSWORD) == 0
    return login("admin@example.com")


@pytest.fixture
def instructor(db, admin):
    from app.models import RegistrationApprovalRequest

    register_verified(db, "teacher@example.com", "Teacher", "INSTRUCTOR")
    req = db.scalars(select(RegistrationApprovalRequest)).first()
    r = admin.post(f"/api/v1/admin/approval-requests/{req.id}/approve", json={"note": "welcome"})
    assert r.status_code == 200, r.text
    return login("teacher@example.com")


@pytest.fixture
def student(db):
    return register_verified(db, "student@example.com", "Student")


@pytest.fixture
def category(admin):
    r = admin.post("/api/v1/categories", json={"name": "AWS DevOps"})
    assert r.status_code == 201, r.text
    return r.json()


def build_course(author: TestClient, category_id: str, price: float = 4999) -> dict:
    r = author.post(
        "/api/v1/courses",
        json={
            "title": "AWS DevOps Bootcamp",
            "category_id": category_id,
            "description": "From Linux to EKS",
            "price_inr": price,
        },
    )
    assert r.status_code == 201, r.text
    course = r.json()
    m = author.post(f"/api/v1/courses/{course['id']}/modules", json={"title": "Linux"}).json()
    f = author.post(f"/api/v1/modules/{m['id']}/folders", json={"title": "Basics"}).json()
    sub = author.post(f"/api/v1/modules/{m['id']}/folders", json={"title": "Permissions", "parent_id": f["id"]}).json()
    text_lesson = author.post(
        "/api/v1/lessons",
        json={
            "module_id": m["id"],
            "folder_id": sub["id"],
            "title": "chmod",
            "lesson_type": "TEXT",
            "content": "chmod 755",
        },
    ).json()
    return {"course": course, "module": m, "folder": f, "subfolder": sub, "text_lesson": text_lesson}


def publish(admin: TestClient, author: TestClient, course_id: str) -> None:
    assert author.post(f"/api/v1/courses/{course_id}/submit-review", json={}).status_code == 200
    assert admin.post(f"/api/v1/admin/courses/{course_id}/approve", json={}).status_code == 200
    r = admin.post(f"/api/v1/admin/courses/{course_id}/publish", json={})
    assert r.status_code == 200, r.text


def buy(admin: TestClient, student: TestClient, course_id: str, price: float = 4999, ref: str = "UPI123456789"):
    from datetime import UTC, datetime

    r = student.post(
        "/api/v1/payments/submit-manual-payment",
        json={
            "course_id": course_id,
            "transaction_reference": ref,
            "amount_inr": price,
            "paid_at": datetime.now(UTC).isoformat(),
        },
    )
    assert r.status_code == 201, r.text
    a = admin.post(f"/api/v1/admin/payments/{r.json()['id']}/approve", json={})
    assert a.status_code == 200, a.text
    return a.json()
