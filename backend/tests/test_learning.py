from tests.conftest import build_course, buy, new_client, publish

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100


def _video_lesson(instructor, storage, b):
    cid = b["course"]["id"]
    r = instructor.post(
        "/api/v1/media/upload-initiate",
        json={"filename": "intro.mp4", "size_bytes": len(MP4), "kind": "VIDEO", "course_id": cid},
    )
    assert r.status_code == 201, r.text
    asset_id = r.json()["asset_id"]
    key = next(iter(storage.uploads.values()))
    storage.simulate_upload(key, MP4)
    r = instructor.post(
        "/api/v1/media/upload-complete", json={"asset_id": asset_id, "parts": [{"part_number": 1, "etag": "x"}]}
    )
    assert r.status_code == 200 and r.json()["status"] == "READY", r.text
    les = instructor.post(
        "/api/v1/lessons", json={"module_id": b["module"]["id"], "title": "Intro", "lesson_type": "VIDEO"}
    ).json()
    r = instructor.patch(f"/api/v1/lessons/{les['id']}", json={"video_asset_id": asset_id})
    assert r.status_code == 200, r.text
    return les, asset_id


def test_upload_rejects_bad_types_and_spoofed_content(instructor, student, storage, category):
    b = build_course(instructor, category["id"])
    cid = b["course"]["id"]
    bad = instructor.post(
        "/api/v1/media/upload-initiate", json={"filename": "x.exe", "size_bytes": 10, "kind": "VIDEO", "course_id": cid}
    )
    assert bad.status_code == 415
    assert (
        student.post(
            "/api/v1/media/upload-initiate",
            json={"filename": "a.mp4", "size_bytes": 10, "kind": "VIDEO", "course_id": cid},
        ).status_code
        == 403
    )
    r = instructor.post(
        "/api/v1/media/upload-initiate",
        json={"filename": "fake.mp4", "size_bytes": 12, "kind": "VIDEO", "course_id": cid},
    )
    storage.simulate_upload(next(iter(storage.uploads.values())), b"MZ" + b"\x00" * 10)
    r = instructor.post(
        "/api/v1/media/upload-complete",
        json={"asset_id": r.json()["asset_id"], "parts": [{"part_number": 1, "etag": "x"}]},
    )
    assert r.status_code == 415


def test_playback_requires_enrollment(admin, instructor, student, storage, category):
    b = build_course(instructor, category["id"])
    les, asset_id = _video_lesson(instructor, storage, b)
    publish(admin, instructor, b["course"]["id"])
    assert new_client().get(f"/api/v1/media/{asset_id}/playback").status_code == 401
    assert student.get(f"/api/v1/media/{asset_id}/playback").status_code == 403
    buy(admin, student, b["course"]["id"])
    r = student.get(f"/api/v1/media/{asset_id}/playback")
    assert r.status_code == 200 and r.json()["expires_in"] <= 3600


def test_progress_resume_and_completion(admin, instructor, student, storage, category, db):
    from app.models import MediaAsset

    b = build_course(instructor, category["id"])
    les, asset_id = _video_lesson(instructor, storage, b)
    db.get(MediaAsset, __import__("uuid").UUID(asset_id)).duration_seconds = 100
    db.commit()
    publish(admin, instructor, b["course"]["id"])
    buy(admin, student, b["course"]["id"])
    cid = b["course"]["id"]
    # A client claiming a huge jump is capped by elapsed real time.
    r = student.put(f"/api/v1/lessons/{les['id']}/progress", json={"position_seconds": 40, "watched_delta_seconds": 40})
    assert r.status_code == 200 and r.json()["completed"] is False
    r = student.put(
        f"/api/v1/lessons/{les['id']}/progress", json={"position_seconds": 95, "watched_delta_seconds": 300}
    )
    assert r.json()["watched_seconds"] < 90 and r.json()["completed"] is False
    assert student.post(f"/api/v1/lessons/{les['id']}/complete").status_code == 409
    p = student.get(f"/api/v1/courses/{cid}/progress").json()
    assert p["resume_lesson_id"] == les["id"] and p["total_required"] == 2 and p["percent"] == 0
    assert student.post(f"/api/v1/lessons/{b['text_lesson']['id']}/complete").status_code == 200
    assert student.get(f"/api/v1/courses/{cid}/progress").json()["percent"] == 50


def test_optional_lessons_excluded_from_progress(admin, instructor, student, category):
    b = build_course(instructor, category["id"])
    instructor.post(
        "/api/v1/lessons",
        json={
            "module_id": b["module"]["id"],
            "title": "Bonus",
            "lesson_type": "TEXT",
            "content": "extra",
            "is_required": False,
        },
    )
    publish(admin, instructor, b["course"]["id"])
    buy(admin, student, b["course"]["id"])
    student.post(f"/api/v1/lessons/{b['text_lesson']['id']}/complete")
    p = student.get(f"/api/v1/courses/{b['course']['id']}/progress").json()
    assert p["total_required"] == 1 and p["percent"] == 100 and p["completed"] is True


def _quiz(instructor, b, max_attempts=2):
    les = instructor.post(
        "/api/v1/lessons", json={"module_id": b["module"]["id"], "title": "Linux quiz", "lesson_type": "QUIZ"}
    ).json()
    q = instructor.get(f"/api/v1/lessons/{les['id']}/quiz/manage").json()
    instructor.patch(f"/api/v1/quizzes/{q['id']}", json={"pass_percentage": 50, "max_attempts": max_attempts})
    for text, correct in [("Which command lists files?", 0), ("Which changes permissions?", 1)]:
        opts = [{"text": t, "is_correct": i == correct} for i, t in enumerate(["ls", "chmod", "pwd"])]
        assert (
            instructor.post(f"/api/v1/quizzes/{q['id']}/questions", json={"text": text, "options": opts}).status_code
            == 201
        )
    assert instructor.post(f"/api/v1/quizzes/{q['id']}/publish").status_code == 200
    return les, instructor.get(f"/api/v1/quizzes/{q['id']}/manage").json()


def test_quiz_hides_answers_grades_server_side_and_limits_attempts(admin, instructor, student, category):
    b = build_course(instructor, category["id"])
    les, quiz = _quiz(instructor, b)
    publish(admin, instructor, b["course"]["id"])
    assert student.get(f"/api/v1/lessons/{les['id']}/quiz").status_code == 403
    buy(admin, student, b["course"]["id"])
    r = student.get(f"/api/v1/lessons/{les['id']}/quiz")
    assert r.status_code == 200
    assert "is_correct" not in r.text and "explanation" not in r.text
    wrong = [{"question_id": q["id"], "option_id": q["options"][2]["id"]} for q in quiz["questions"]]
    a1r = student.post(f"/api/v1/quizzes/{quiz['id']}/attempts", json={"answers": wrong})
    assert a1r.status_code == 201, a1r.text
    a1 = a1r.json()
    assert a1["score"] == 0 and a1["passed"] is False
    right = [
        {"question_id": q["id"], "option_id": next(o["id"] for o in q["options"] if o["is_correct"])}
        for q in quiz["questions"]
    ]
    a2 = student.post(f"/api/v1/quizzes/{quiz['id']}/attempts", json={"answers": right}).json()
    assert a2["percentage"] == 100 and a2["passed"] is True and a2["attempt_number"] == 2
    r = student.post(f"/api/v1/quizzes/{quiz['id']}/attempts", json={"answers": right})
    assert r.status_code == 409 and r.json()["error"]["code"] == "attempts_exhausted"
    stats = instructor.get(f"/api/v1/quizzes/{quiz['id']}/analytics").json()
    assert stats["attempts"] == 2 and stats["pass_rate"] == 50


def test_notifications_after_payment_approval(admin, instructor, student, category):
    b = build_course(instructor, category["id"])
    publish(admin, instructor, b["course"]["id"])
    buy(admin, student, b["course"]["id"])
    items = student.get("/api/v1/notifications").json()["items"]
    assert any(n["type"] == "payment.approved" for n in items)
    assert student.get("/api/v1/notifications/unread-count").json()["count"] >= 1
    assert student.post("/api/v1/notifications/read-all").status_code == 200
    assert student.get("/api/v1/notifications/unread-count").json()["count"] == 0
