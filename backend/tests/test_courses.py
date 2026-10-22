from tests.conftest import build_course, new_client, publish, register_verified


def test_instructor_builds_nested_curriculum(instructor, category):
    b = build_course(instructor, category["id"])
    tree = instructor.get(f"/api/v1/courses/{b['course']['id']}/curriculum").json()
    assert tree["modules"][0]["folders"][0]["folders"][0]["lessons"][0]["title"] == "chmod"


def test_folder_cannot_move_into_its_descendant(instructor, category):
    b = build_course(instructor, category["id"])
    r = instructor.patch(f"/api/v1/folders/{b['folder']['id']}", json={"parent_id": b["subfolder"]["id"], "move": True})
    assert r.status_code == 400 and r.json()["error"]["code"] == "folder_cycle"


def test_other_instructor_cannot_touch_course(db, admin, instructor, category):
    from sqlalchemy import select

    from app.models import RegistrationApprovalRequest
    from tests.conftest import login

    b = build_course(instructor, category["id"])
    register_verified(db, "other@example.com", "Other", "INSTRUCTOR")
    req = db.scalars(select(RegistrationApprovalRequest).where(RegistrationApprovalRequest.status == "PENDING")).one()
    admin.post(f"/api/v1/admin/approval-requests/{req.id}/approve", json={})
    other = login("other@example.com")
    cid = b["course"]["id"]
    assert other.patch(f"/api/v1/courses/{cid}", json={"title": "Stolen"}).status_code in (403, 404)
    assert other.post(f"/api/v1/courses/{cid}/modules", json={"title": "X"}).status_code in (403, 404)
    assert other.get(f"/api/v1/courses/{cid}/curriculum").status_code in (403, 404)


def test_student_cannot_create_courses(student, category):
    r = student.post("/api/v1/courses", json={"title": "Nope", "category_id": category["id"]})
    assert r.status_code == 403


def test_publication_lifecycle_and_catalog_visibility(admin, instructor, category):
    b = build_course(instructor, category["id"])
    cid = b["course"]["id"]
    assert new_client().get("/api/v1/courses").json()["total"] == 0
    assert new_client().get(f"/api/v1/courses/{cid}").status_code == 404
    # Instructors cannot approve or publish their own course.
    assert instructor.post(f"/api/v1/admin/courses/{cid}/publish", json={}).status_code == 403
    assert instructor.post(f"/api/v1/courses/{cid}/submit-review", json={}).status_code == 200
    # Locked while under review.
    assert instructor.patch(f"/api/v1/courses/{cid}", json={"title": "Changed title"}).status_code == 409
    r = admin.post(f"/api/v1/admin/courses/{cid}/request-changes", json={})
    assert r.status_code == 400  # comment required
    assert admin.post(f"/api/v1/admin/courses/{cid}/request-changes", json={"comment": "Add more"}).status_code == 200
    publish(admin, instructor, cid)
    catalog = new_client().get("/api/v1/courses").json()
    assert catalog["total"] == 1 and catalog["items"][0]["status"] == "PUBLISHED"
    actions = [x["action"] for x in instructor.get(f"/api/v1/courses/{cid}/reviews").json()]
    assert {"submit", "request_changes", "approve", "publish"} <= set(actions)


def test_cannot_submit_empty_course(instructor, category):
    r = instructor.post("/api/v1/courses", json={"title": "Empty", "category_id": category["id"], "description": "x"})
    assert instructor.post(f"/api/v1/courses/{r.json()['id']}/submit-review", json={}).status_code == 400


def test_public_outline_hides_lesson_content(admin, instructor, category):
    b = build_course(instructor, category["id"])
    publish(admin, instructor, b["course"]["id"])
    r = new_client().get(f"/api/v1/courses/{b['course']['slug']}")
    assert r.status_code == 200
    assert "chmod 755" not in r.text
    assert new_client().get(f"/api/v1/lessons/{b['text_lesson']['id']}").status_code in (401, 403, 404)


def test_category_with_courses_cannot_be_deleted(admin, instructor, category):
    build_course(instructor, category["id"])
    assert admin.delete(f"/api/v1/categories/{category['id']}").status_code == 409
