import shutil
import threading
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


def test_baidu_translation_request_and_error(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    import hashlib
    import httpx

    monkeypatch.setenv("BAIDU_TRANSLATE_APP_ID", "test-app")
    monkeypatch.setenv("BAIDU_TRANSLATE_SECRET", "test-secret")
    calls = []

    def fake_post(url, data, timeout):
        calls.append((url, data))
        return httpx.Response(200, json={"trans_result": [{"dst": "你好"}]}, request=httpx.Request("POST", url))

    monkeypatch.setattr(server.httpx, "post", fake_post)
    assert server.translate_text("こんにちは", "ja", "zh") == "你好"
    url, data = calls[0]
    assert url == "https://fanyi-api.baidu.com/api/trans/vip/translate"
    assert (data["from"], data["to"]) == ("jp", "zh")
    assert data["sign"] == hashlib.md5(("test-app" + data["q"] + data["salt"] + "test-secret").encode()).hexdigest()
    monkeypatch.setattr(server.httpx, "post", lambda url, data, timeout: httpx.Response(200, json={"error_code": "52003"}, request=httpx.Request("POST", url)))
    lesson = client.post("/api/lessons/demo").json()
    sentence = client.get(f"/api/lessons/{lesson['id']}").json()["sentences"][0]
    response = client.post(f"/api/sentences/{sentence['id']}/translate", json={"target_language": "ja"})
    assert response.status_code == 502
    assert "52003" in response.json()["detail"]


def test_dictionary_returns_pronunciation_and_translated_definitions(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    import httpx

    def fake_get(url, timeout, params=None):
        if "dictionaryapi.dev" in url:
            assert url.endswith("/ideas")
            payload = [{
                "word": "ideas",
                "phonetics": [
                    {"text": "/aɪˈdiːəz/", "audio": "https://audio.example/ideas-us.mp3"},
                    {"text": "/aɪˈdɪəz/", "audio": "https://audio.example/ideas-uk.mp3"},
                ],
                "meanings": [{"partOfSpeech": "noun", "definitions": [{"definition": "A thought or suggestion.", "example": "Her ideas changed the plan."}, {"definition": "A concept.", "example": "We shared three ideas."}, {"definition": "An opinion.", "example": "That idea is useful."}]}],
            }]
        else:
            payload = [{"word": "ideas", "defs": ["N\tIdeas usually refer to thoughts or concepts."]}]
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(server.httpx, "get", fake_get)
    monkeypatch.setattr(server, "translate_text", lambda text, source, target, context=None: "想法；建议")
    result = client.post(
        "/api/dictionary",
        json={"word": "ideas.", "source_language": "en", "target_language": "zh"},
    )
    assert result.status_code == 200
    assert result.json()["meaning"] == "想法；建议"
    assert result.json()["part_of_speech_label"] == "n."
    assert len(result.json()["phonetics"]) == 2
    assert len(result.json()["examples"]) == 3
    assert result.json()["phonetics"][0]["region"] == "美式"


def test_rename_and_delete_lesson_removes_audio(client: TestClient):
    created = client.post(
        "/api/lessons",
        content=b"audio",
        headers={
            "content-type": "audio/wav",
            "x-file-name": "menu.wav",
            "x-title": "Old name",
            "x-source-language": "en",
        },
    ).json()
    renamed = client.patch(f"/api/lessons/{created['id']}", json={"title": "New name"})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "New name"
    assert client.delete(f"/api/lessons/{created['id']}").status_code == 200
    assert client.get(f"/api/lessons/{created['id']}").status_code == 404
    assert client.get(created["audio_url"]).status_code == 404


def test_word_lookup_is_saved_and_listed(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "translate_text", lambda word, source, target, context=None: "学习")
    lesson = client.post("/api/lessons/demo").json()
    first = client.get(f"/api/lessons/{lesson['id']}").json()["sentences"][0]
    response = client.post(
        "/api/lookup",
        json={"sentence_id": first["id"], "word": "learning", "target_language": "zh", "save": False},
    )
    assert response.status_code == 200
    assert response.json()["meaning"] == "学习"
    assert response.json()["saved"] is False
    assert client.get("/api/vocabulary").json() == []
    saved = client.post(
        "/api/vocabulary",
        json={"sentence_id": first["id"], "word": "learning", "target_language": "zh", "meaning": "学习"},
    )
    assert saved.status_code == 200
    assert saved.json()["saved"] is True
    listed = client.get("/api/vocabulary").json()
    assert len(listed) == 1
    assert listed[0]["lesson_title"] == "每日精听 · 示例课程"
    assert client.post(
        "/api/lookup",
        json={"sentence_id": first["id"], "word": "learning", "target_language": "zh", "save": False},
    ).json()["saved"] is True
    assert client.delete(f"/api/vocabulary/{saved.json()['id']}").status_code == 200


def test_recognition_model_switch_supports_idle_wait_and_cancel(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    loaded = []
    monkeypatch.setattr(server, "model_is_available", lambda name: True)

    def fake_load(name):
        loaded.append(name)
        monkeypatch.setattr(server, "_selected_model", name)

    monkeypatch.setattr(server, "load_whisper_model", fake_load)
    monkeypatch.setattr(server, "_active_transcriptions", {})
    monkeypatch.setattr(server, "_pending_model", None)
    monkeypatch.setattr(server, "_model_switching", False)

    immediate = client.post("/api/recognition/model", json={"model": "base", "behavior": "immediate"})
    assert immediate.status_code == 200
    assert loaded == ["base"]

    waiting = threading.Event()
    monkeypatch.setattr(server, "_active_transcriptions", {"lesson-wait": waiting})
    unavailable_immediate = client.post("/api/recognition/model", json={"model": "small", "behavior": "immediate"})
    assert unavailable_immediate.status_code == 409
    wait_response = client.post("/api/recognition/model", json={"model": "small", "behavior": "wait"})
    assert wait_response.status_code == 200
    assert wait_response.json()["pending_model"] == "small"
    monkeypatch.setattr(server, "_active_transcriptions", {})
    server.complete_pending_model_switch()
    assert loaded[-1] == "small"

    cancel_event = threading.Event()
    monkeypatch.setattr(server, "_active_transcriptions", {"lesson-cancel": cancel_event})
    cancel_response = client.post("/api/recognition/model", json={"model": "tiny", "behavior": "cancel"})
    assert cancel_response.status_code == 200
    assert cancel_event.is_set()
    assert cancel_response.json()["pending_model"] == "tiny"
    monkeypatch.setattr(server, "_active_transcriptions", {})
    server.complete_pending_model_switch()
    assert loaded[-1] == "tiny"


def test_reparse_queues_current_model_again(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    lesson = client.post("/api/lessons/demo").json()
    calls = []
    monkeypatch.setattr(server, "start_transcription", lambda lesson_id, force=False: calls.append((lesson_id, force)) or {"status": "processing"})
    result = client.post(f"/api/lessons/{lesson['id']}/reparse")
    assert result.status_code == 200
    assert calls == [(lesson["id"], True)]
    refreshed = client.get(f"/api/lessons/{lesson['id']}").json()["lesson"]
    assert refreshed["status"] == "uploaded"
    assert "重新解析" in refreshed["progress_message"]


def test_vocabulary_detail_delete_and_force_translation(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    lesson = client.post("/api/lessons/demo").json()
    sentence = client.get(f"/api/lessons/{lesson['id']}").json()["sentences"][0]
    saved = client.post("/api/vocabulary", json={"sentence_id": sentence["id"], "word": "learning", "target_language": "zh", "meaning": "学习"}).json()
    detail = client.get(f"/api/vocabulary/{saved['id']}")
    assert detail.status_code == 200
    assert detail.json()["sentence_text"].startswith("I used to think")
    calls = []
    monkeypatch.setattr(server, "translate_text", lambda text, source, target, context=None: calls.append(text) or "新译文")
    first = client.post(f"/api/sentences/{sentence['id']}/translate", json={"target_language": "zh"})
    second = client.post(f"/api/sentences/{sentence['id']}/translate", json={"target_language": "zh", "force": True})
    assert first.status_code == second.status_code == 200
    assert len(calls) == 1
    assert second.json()["translated_text"] == "新译文"
    assert client.delete(f"/api/vocabulary/{saved['id']}").status_code == 200
    assert client.get(f"/api/vocabulary/{saved['id']}").status_code == 404
