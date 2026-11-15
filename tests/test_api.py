import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    temp_dir = Path(__file__).resolve().parent.parent / ".test-data" / uuid.uuid4().hex
    temp_dir.mkdir(parents=True)
    monkeypatch.setattr(server, "DB_PATH", temp_dir / "study.sqlite3")
    monkeypatch.setattr(server, "UPLOAD_DIR", temp_dir / "uploads")
    server.UPLOAD_DIR.mkdir()
    server.initialize_database()
    try:
        with TestClient(server.app) as test_client:
            yield test_client
    finally:
        shutil.rmtree(temp_dir)


def test_demo_lesson_and_cached_translation(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    demo = client.post("/api/lessons/demo").json()
    detail = client.get(f"/api/lessons/{demo['id']}").json()
    assert detail["lesson"]["title"] == "每日精听 · 示例课程"
    assert len(detail["sentences"]) == 4
    first = detail["sentences"][0]
    result = client.post(
        f"/api/sentences/{first['id']}/translate", json={"target_language": "zh"}
    )
    assert result.status_code == 200
    assert result.json()["translated_text"].startswith("我过去以为")
    edited = client.patch(
        f"/api/sentences/{first['id']}",
        json={"text": "A different sentence.", "start_ms": 0, "end_ms": 4000},
    )
    assert edited.status_code == 200
    monkeypatch.setattr(server, "translate_text", lambda text, source, target, context=None: "新译文")
    refreshed = client.post(
        f"/api/sentences/{first['id']}/translate", json={"target_language": "zh"}
    )
    assert refreshed.json()["translated_text"] == "新译文"
    assert refreshed.json()["source_text"] == "A different sentence."


def test_edit_split_and_merge_sentence(client: TestClient):
    lesson = client.post("/api/lessons/demo").json()
    detail = client.get(f"/api/lessons/{lesson['id']}").json()
    first = detail["sentences"][0]
    edited = client.patch(
        f"/api/sentences/{first['id']}",
        json={"text": "A corrected sentence.", "start_ms": 10, "end_ms": 5100},
    )
    assert edited.status_code == 200
    assert edited.json()["edited_text"] == "A corrected sentence."
    split = client.post(f"/api/sentences/{first['id']}/split", json={"offset": 10})
    assert split.status_code == 200
    updated = client.get(f"/api/lessons/{lesson['id']}").json()["sentences"]
    assert len(updated) == 5
    merged = client.post(f"/api/sentences/{first['id']}/merge-next")
    assert merged.status_code == 200
    assert len(client.get(f"/api/lessons/{lesson['id']}").json()["sentences"]) == 4


def test_raw_audio_upload_is_private_and_validated(client: TestClient):
    response = client.post(
        "/api/lessons",
        content=b"not-a-real-wave-file",
        headers={
            "content-type": "audio/wav",
            "x-file-name": "practice.wav",
            "x-title": "Practice",
            "x-source-language": "en",
        },
    )
    assert response.status_code == 200
    lesson = response.json()
    assert lesson["title"] == "Practice"
    audio = client.get(lesson["audio_url"])
    assert audio.status_code == 200
    assert audio.content == b"not-a-real-wave-file"


def test_unsupported_audio_extension_is_rejected(client: TestClient):
    response = client.post(
        "/api/lessons",
        content=b"data",
        headers={
            "content-type": "application/octet-stream",
            "x-file-name": "practice.exe",
            "x-source-language": "en",
        },
    )
    assert response.status_code == 400


def test_vocabulary_starts_empty(client: TestClient):
    assert client.get("/api/vocabulary").json() == []


def test_word_lookup_is_saved_and_listed(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "translate_text", lambda word, source, target, context=None: "学习")
    lesson = client.post("/api/lessons/demo").json()
    first = client.get(f"/api/lessons/{lesson['id']}").json()["sentences"][0]
    response = client.post(
        "/api/lookup",
        json={"sentence_id": first["id"], "word": "learning", "target_language": "zh"},
    )
    assert response.status_code == 200
    assert response.json()["meaning"] == "学习"
    listed = client.get("/api/vocabulary").json()
    assert len(listed) == 1
    assert listed[0]["lesson_title"] == "每日精听 · 示例课程"
