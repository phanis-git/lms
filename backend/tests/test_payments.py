from datetime import UTC, datetime

from tests.conftest import build_course, buy, publish


def _setup(admin, instructor, category):
    b = build_course(instructor, category["id"])
    publish(admin, instructor, b["course"]["id"])
    return b


def _submit(student, cid, ref="UPI987654321", amount=4999):
    return student.post(
        "/api/v1/payments/submit-manual-payment",
        json={
            "course_id": cid,
            "transaction_reference": ref,
            "amount_inr": amount,
            "paid_at": datetime.now(UTC).isoformat(),
        },
    )


def test_content_locked_until_payment_verified(admin, instructor, student, category):
    b = _setup(admin, instructor, category)
    cid, lid = b["course"]["id"], b["text_lesson"]["id"]
    assert student.get(f"/api/v1/lessons/{lid}").status_code == 403
    r = _submit(student, cid)
    assert r.status_code == 201 and r.json()["status"] == "PENDING_VERIFICATION"
    assert student.get(f"/api/v1/lessons/{lid}").status_code == 403
    assert student.get(f"/api/v1/courses/{cid}/enrollment-status").json()["pending_payment"] is True
    a = admin.post(f"/api/v1/admin/payments/{r.json()['id']}/approve", json={})
    assert a.status_code == 200
    assert student.get(f"/api/v1/lessons/{lid}").status_code == 200
    assert student.get("/api/v1/enrollments/my-courses").json()[0]["course_id"] == cid
    # Approving twice is rejected and does not duplicate enrollments.
    assert admin.post(f"/api/v1/admin/payments/{r.json()['id']}/approve", json={}).status_code == 409


def test_duplicate_reference_and_amount_checks(db, admin, instructor, student, category):
    from tests.conftest import register_verified

    b = _setup(admin, instructor, category)
    cid = b["course"]["id"]
    assert _submit(student, cid, amount=10).json()["error"]["code"] == "amount_mismatch"
    assert _submit(student, cid).status_code == 201
    assert _submit(student, cid, ref="UPI000000001").json()["error"]["code"] == "payment_pending"
    other = register_verified(db, "other-student@example.com")
    r = _submit(other, cid, ref="upi 987654321")  # normalised to the same reference
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_reference"


def test_reject_requires_reason_and_allows_resubmission(admin, instructor, student, category):
    b = _setup(admin, instructor, category)
    pid = _submit(student, b["course"]["id"]).json()["id"]
    assert admin.post(f"/api/v1/admin/payments/{pid}/reject", json={}).status_code == 400
    r = admin.post(f"/api/v1/admin/payments/{pid}/reject", json={"reason": "Reference not found in bank statement"})
    assert r.json()["status"] == "REJECTED"
    st = student.get(f"/api/v1/courses/{b['course']['id']}/enrollment-status").json()
    assert st["enrolled"] is False and "bank statement" in st["last_rejection_reason"]
    # The same reference can be re-submitted after a rejection (e.g. a typo was fixed).
    assert _submit(student, b["course"]["id"]).status_code == 201


def test_students_cannot_verify_payments(admin, instructor, student, category):
    b = _setup(admin, instructor, category)
    pid = _submit(student, b["course"]["id"]).json()["id"]
    assert student.post(f"/api/v1/admin/payments/{pid}/approve", json={}).status_code == 403
    assert student.get("/api/v1/admin/payments").status_code == 403


def test_cannot_pay_for_unpublished_course(instructor, student, category):
    b = build_course(instructor, category["id"])
    assert _submit(student, b["course"]["id"]).status_code in (404, 409)


def test_refund_revokes_access(admin, instructor, student, category):
    b = _setup(admin, instructor, category)
    p = buy(admin, student, b["course"]["id"])
    r = admin.post(f"/api/v1/admin/payments/{p['id']}/refund", json={"reason": "Requested within 7 days"})
    assert r.status_code == 200 and r.json()["status"] == "REFUNDED"
    assert student.get(f"/api/v1/lessons/{b['text_lesson']['id']}").status_code == 403
