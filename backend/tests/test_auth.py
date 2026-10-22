from sqlalchemy import select

from tests.conftest import PASSWORD, latest_email, login, new_client, register_verified


def test_register_requires_verification_then_login(db):
    c = new_client()
    r = c.post("/api/v1/auth/register", json={"email": "Ann@Example.com", "password": PASSWORD, "full_name": "Ann"})
    assert r.status_code == 201
    r = c.post("/api/v1/auth/login", json={"email": "ann@example.com", "password": PASSWORD})
    assert r.status_code == 403 and r.json()["error"]["code"] == "email_not_verified"
    assert "verify-email?token=" in latest_email(db, "ann@example.com").body_text


def test_duplicate_email_rejected(db, student):
    r = new_client().post(
        "/api/v1/auth/register", json={"email": "STUDENT@example.com", "password": PASSWORD, "full_name": "Dup"}
    )
    assert r.status_code == 409


def test_weak_password_rejected():
    r = new_client().post(
        "/api/v1/auth/register", json={"email": "w@example.com", "password": "short", "full_name": "W"}
    )
    assert r.status_code == 422


def test_wrong_password_generic_error(student):
    r = new_client().post("/api/v1/auth/login", json={"email": "student@example.com", "password": "Wrong12345!"})
    assert r.status_code == 401
    r2 = new_client().post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "Wrong12345!"})
    assert r2.status_code == 401 and r.json()["error"]["message"] == r2.json()["error"]["message"]


def test_me_and_logout(student):
    assert student.get("/api/v1/auth/me").json()["user"]["role"] == "STUDENT"
    assert student.post("/api/v1/auth/logout").status_code == 200
    assert student.get("/api/v1/auth/me").status_code == 401


def test_csrf_required_for_unsafe_methods(student):
    del student.headers["X-XSRF-TOKEN"]
    r = student.patch("/api/v1/users/me", json={"full_name": "Hacker"})
    assert r.status_code == 403


def test_requesting_admin_role_does_not_grant_it(db, admin):
    from app.models import RegistrationApprovalRequest

    c = register_verified(db, "wannabe@example.com", "Wannabe", "ADMIN")
    assert c.user["role"] == "STUDENT"
    req = db.scalars(select(RegistrationApprovalRequest)).one()
    assert req.requested_role.value == "ADMIN" and req.status.value == "PENDING"
    assert latest_email(db, "sivaphanikumar.tech@gmail.com") is not None
    assert c.get("/api/v1/admin/users").status_code == 403


def test_admin_approval_grants_instructor_and_audits(db, instructor, admin):
    assert instructor.user["role"] == "INSTRUCTOR"
    logs = admin.get("/api/v1/admin/audit-logs").json()["items"]
    assert any(log["action"].startswith("approval.") for log in logs)


def test_student_cannot_approve_requests(db, student, admin):
    from app.models import RegistrationApprovalRequest

    register_verified(db, "t2@example.com", "T2", "INSTRUCTOR")
    req = db.scalars(select(RegistrationApprovalRequest)).one()
    assert student.post(f"/api/v1/admin/approval-requests/{req.id}/approve", json={}).status_code == 403


def test_last_admin_cannot_be_demoted_or_self_suspended(admin):
    me = admin.user["id"]
    assert admin.patch(f"/api/v1/admin/users/{me}/role", json={"role": "STUDENT"}).status_code in (400, 409)
    assert admin.patch(f"/api/v1/admin/users/{me}/status", json={"status": "SUSPENDED"}).status_code in (400, 409)


def test_suspension_revokes_sessions(admin, student):
    sid = student.user["id"]
    assert (
        admin.patch(f"/api/v1/admin/users/{sid}/status", json={"status": "SUSPENDED", "reason": "x"}).status_code == 200
    )
    assert student.get("/api/v1/auth/me").status_code == 401
    r = new_client().post("/api/v1/auth/login", json={"email": "student@example.com", "password": PASSWORD})
    assert r.status_code == 403


def test_password_reset_flow_revokes_sessions(db, student):
    import re

    c = new_client()
    assert c.post("/api/v1/auth/forgot-password", json={"email": "student@example.com"}).status_code == 200
    # Unknown emails get the same response (no account enumeration).
    assert c.post("/api/v1/auth/forgot-password", json={"email": "ghost@example.com"}).status_code == 200
    token = re.search(r"token=([\w\-]+)", latest_email(db, "student@example.com").body_text).group(1)
    new_pw = "An0therStrongPass"
    assert c.post("/api/v1/auth/reset-password", json={"token": token, "password": new_pw}).status_code == 200
    assert c.post("/api/v1/auth/reset-password", json={"token": token, "password": new_pw}).status_code == 400
    assert student.get("/api/v1/auth/me").status_code == 401
    login("student@example.com", new_pw)
