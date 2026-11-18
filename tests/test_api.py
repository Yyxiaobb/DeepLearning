import json
import shutil
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    temp_dir = Path(__file__).resolve().parent.parent / ".test-data" / uuid.uuid4().hex
    temp_dir.mkdir(parents=True)
    monkeypatch.setattr(server, "DB_PATH", temp_dir / "study.sqlite3")
    monkeypatch.setattr(server, "UPLOAD_DIR", temp_dir / "uploads")
    monkeypatch.setattr(server, "queue_vocabulary_generation", lambda vocabulary_id: None)
    monkeypatch.setattr(server, "dictionary_payload_for_lookup", lambda word, source, target: None)
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


def test_sentence_split_uses_terminal_punctuation_and_keeps_quotes_attached():
    assert server.split_sentences(
        'He said, "Wait: not yet!" she replied. Then we left.'
    ) == ['He said, "Wait: not yet!" she replied.', "Then we left."]
    assert server.split_sentences(
        'She asked, "Are you ready: now?" I smiled!'
    ) == ['She asked, "Are you ready: now?"', "I smiled!"]
    assert server.split_sentences(
        'She asked, "Ready?" and waited. Then we left.'
    ) == ['She asked, "Ready?" and waited.', "Then we left."]
    assert server.split_sentences('"Are you ready?" I asked.') == [
        '"Are you ready?" I asked.'
    ]
    assert server.split_sentences("他说：‘准备好了吗？’她点头，然后离开。") == [
        "他说：‘准备好了吗？’她点头，然后离开。"
    ]
    assert server.split_sentences("Use 3.14, e.g. this value. Then continue!") == [
        "Use 3.14, e.g. this value.", "Then continue!"
    ]
    assert server.split_sentences("First!Second?") == ["First!", "Second?"]


def test_whisper_chunks_are_joined_before_sentence_splitting_with_times():
    chunks = [
        SimpleNamespace(text="At the first demonstration, a group of", start=1.0, end=3.0),
        SimpleNamespace(text="Americans promised to work for change.", start=3.2, end=6.0),
        SimpleNamespace(text="They later went home.", start=6.4, end=8.0),
    ]
    sentences = server.split_segments_with_times(chunks)
    assert [item[0] for item in sentences] == [
        "At the first demonstration, a group of Americans promised to work for change.",
        "They later went home.",
    ]
    assert sentences[0][1] == 1000
    assert sentences[0][2] <= sentences[1][1]
    assert sentences[1][2] == 8000


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

    monkeypatch.setenv("VOCAB_LLM_BASE_URL", "")
    monkeypatch.setenv("VOCAB_LLM_MODEL", "")

    def fake_get(url, timeout, params=None):
        if "dictionaryapi.dev" in url:
            assert url.endswith("/ideas")
            payload = [{
                "word": "ideas",
                "phonetics": [
                    {"text": "/aɪˈdiːəz/", "audio": "https://audio.example/ideas-us.mp3"},
                    {"text": "/aɪˈdɪəz/", "audio": "https://audio.example/ideas-uk.mp3"},
                    {"text": "/aɪˈdɪəz/", "audio": "https://audio.example/ideas-au.mp3"},
                ],
                "meanings": [
                    {"partOfSpeech": "noun", "definitions": [{"definition": "A thought or suggestion.", "example": "Her ideas changed the plan."}, {"definition": "A concept.", "example": "We shared three ideas."}, {"definition": "An opinion.", "example": "That idea is useful."}]},
                    {"partOfSpeech": "verb", "definitions": [{"definition": "To form a thought or plan.", "example": "She ideas a new project."}]},
                ],
            }]
        else:
            payload = [{"word": "ideas", "defs": ["N\tIdeas usually refer to thoughts or concepts."]}]
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(server.httpx, "get", fake_get)
    monkeypatch.setattr(server, "translate_text", lambda text, source, target, context=None: "想法；建议")
    monkeypatch.setattr(server, "translate_text_with_provider", lambda text, source, target, context=None: ("想法；建议", "baidu_api"))
    result = client.post(
        "/api/dictionary",
        json={"word": "ideas.", "source_language": "en", "target_language": "zh"},
    )
    assert result.status_code == 200
    assert result.json()["meaning"] == "想法；建议"
    assert result.json()["part_of_speech_label"] == "n."
    assert len(result.json()["phonetics"]) == 3
    assert len(result.json()["examples"]) == 3
    assert result.json()["phonetics"][0]["region"] == "美式"
    assert result.json()["phonetics"][2]["region"] == "澳式"
    assert len(result.json()["definition_items"]) == 4
    assert result.json()["definition_items"][3]["part_of_speech"] == "verb"
    brief_items = result.json()["brief_items"]
    assert [item["part_of_speech"] for item in brief_items] == ["noun", "verb"]
    assert all(item["provider"] == "baidu_api" for item in brief_items)


def test_dictionary_brief_items_keep_all_senses_grouped_by_part_of_speech(monkeypatch: pytest.MonkeyPatch):
    import json

    definitions = [
        {"part_of_speech": "noun", "text": "A thought or suggestion."},
        {"part_of_speech": "noun", "text": "A concept."},
        {"part_of_speech": "verb", "text": "To form a thought or plan."},
    ]

    def fake_local_model(messages, max_tokens, response_format=None):
        request = json.loads(messages[-1]["content"])
        assert [item["sense_id"] for item in request["senses"]] == [0, 1, 2]
        return '{"items":[{"sense_id":0,"gloss":"想法或建议"},{"sense_id":1,"gloss":"概念"},{"sense_id":2,"gloss":"构思"}]}'

    monkeypatch.setattr(server, "local_model_chat", fake_local_model)
    result = server.build_chinese_dictionary_brief_items("ideas", definitions, [], True, "", "")

    assert result == [
        {"part_of_speech": "noun", "text": "想法或建议；概念", "provider": "local_model"},
        {"part_of_speech": "verb", "text": "构思", "provider": "local_model"},
    ]


def test_dictionary_batches_chinese_glosses_and_examples_in_one_model_call(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    import json
    import httpx

    monkeypatch.setenv("VOCAB_LLM_BASE_URL", "http://local-model.test/v1")
    monkeypatch.setenv("VOCAB_LLM_MODEL", "test-model")
    payload = [{
        "word": "apple",
        "phonetics": [],
        "meanings": [
            {"partOfSpeech": "noun", "definitions": [
                {"definition": "A round fruit with crisp skin.", "example": "She ate the apple after lunch."},
                {"definition": "The tree that bears this fruit.", "example": "The apple tree grows here."},
            ]},
            {"partOfSpeech": "verb", "definitions": [
                {"definition": "To entice someone with an offer."},
            ]},
        ],
    }]
    monkeypatch.setattr(server.httpx, "get", lambda url, timeout, params=None: httpx.Response(200, json=payload, request=httpx.Request("GET", url)))
    calls = []

    def fake_local_model(messages, max_tokens, response_format=None):
        request = json.loads(messages[-1]["content"])
        calls.append(request)
        return json.dumps({
            "senses": [
                {"sense_id": 0, "gloss": "苹果"},
                {"sense_id": 1, "gloss": "苹果树"},
                {"sense_id": 2, "gloss": "诱惑"},
            ],
            "examples": [
                {"example_id": 0, "translation": "她午饭后吃了苹果。"},
                {"example_id": 1, "translation": "苹果树在这里生长。"},
            ],
        }, ensure_ascii=False)

    monkeypatch.setattr(server, "local_model_chat", fake_local_model)
    monkeypatch.setattr(server, "translate_text_with_provider", lambda *args, **kwargs: pytest.fail("批处理成功时不应重复翻译"))
    monkeypatch.setattr(server, "translate_text", lambda *args, **kwargs: pytest.fail("例句译文应复用批处理结果"))

    result = client.post("/api/dictionary", json={"word": "apple", "source_language": "en", "target_language": "zh"})

    assert result.status_code == 200
    assert len(calls) == 1
    assert result.json()["brief_items"] == [
        {"part_of_speech": "noun", "text": "苹果；苹果树", "provider": "local_model"},
        {"part_of_speech": "verb", "text": "诱惑", "provider": "local_model"},
    ]
    assert [item["translation"] for item in result.json()["example_items"]] == ["她午饭后吃了苹果。", "苹果树在这里生长。"]


def test_dictionary_results_are_cached_in_sqlite(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    import httpx

    monkeypatch.setenv("VOCAB_LLM_BASE_URL", "")
    monkeypatch.setenv("VOCAB_LLM_MODEL", "")
    calls = {"dictionary": 0, "translation": 0}
    payload = [{
        "word": "notion",
        "phonetics": [],
        "meanings": [{"partOfSpeech": "noun", "definitions": [{"definition": "An idea or belief.", "example": "She had a notion about the plan."}]}],
    }]

    def fake_get(url, timeout, params=None):
        calls["dictionary"] += 1
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    def fake_translate(text, source, target, context=None):
        calls["translation"] += 1
        return "想法", "baidu_api"

    monkeypatch.setattr(server.httpx, "get", fake_get)
    monkeypatch.setattr(server, "translate_text_with_provider", fake_translate)
    monkeypatch.setattr(server, "translate_text", lambda *args, **kwargs: "她对计划有个想法。")
    body = {"word": "notion", "source_language": "en", "target_language": "zh"}

    first = client.post("/api/dictionary", json=body)
    first_translation_calls = calls["translation"]
    second = client.post("/api/dictionary", json=body)

    assert first.status_code == second.status_code == 200
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert calls["dictionary"] == 1
    assert calls["translation"] == first_translation_calls
    with server.db() as conn:
        conn.execute("UPDATE dictionary_cache SET cached_at='2000-01-01T00:00:00+00:00'")
    third = client.post("/api/dictionary", json=body)
    assert third.status_code == 200
    assert third.json()["cached"] is True
    assert calls["dictionary"] == 1


def test_context_word_translation_prompt_keeps_only_selected_word(monkeypatch: pytest.MonkeyPatch):
    captured = {}

    def fake_chat(messages, max_tokens, response_format=None):
        captured["system"] = messages[0]["content"]
        captured["user"] = json.loads(messages[1]["content"])
        return "大学"

    monkeypatch.setattr(server, "local_model_chat", fake_chat)
    monkeypatch.setenv("VOCAB_LLM_BASE_URL", "http://local-model.test/v1")
    monkeypatch.setenv("VOCAB_LLM_MODEL", "test-model")

    result = server.translate_text_with_model(
        "University", "en", "zh", "Listen The Easy Way, published by Fudan University Press."
    )

    assert result == "大学"
    assert captured["user"] == {
        "word_or_phrase": "University",
        "context": "Listen The Easy Way, published by Fudan University Press.",
    }
    assert "不能输出“复旦大学出版社”" in captured["system"]


def test_vocabulary_detail_keeps_dictionary_pos_groups_and_provenance(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    demo = client.post("/api/lessons/demo").json()
    sentence = client.get(f"/api/lessons/{demo['id']}").json()["sentences"][0]
    monkeypatch.setattr(server, "queue_vocabulary_generation", lambda vocabulary_id: None)
    saved = client.post("/api/vocabulary", json={
        "sentence_id": sentence["id"], "word": "ideas", "target_language": "zh", "meaning": "想法",
    }).json()
    brief_items = [
        {"part_of_speech": "noun", "text": "想法；概念", "provider": "local_model"},
        {"part_of_speech": "verb", "text": "构思", "provider": "baidu_api"},
    ]
    dictionary_result = {
        "brief_items": brief_items, "phonetics": [],
        "example_items": [{"text": "Her ideas changed the plan.", "translation": "她的想法改变了计划。"}],
    }
    requested_targets = []
    monkeypatch.setattr(server, "dictionary_word", lambda body: (requested_targets.append(body.target_language), dictionary_result)[1])
    monkeypatch.setattr(server, "generate_vocabulary_with_model", lambda row, dictionary: None)
    monkeypatch.setattr(server, "cache_pronunciation_audio", lambda *args: "")

    server.generate_vocabulary_job(saved["id"])
    detail = client.get(f"/api/vocabulary/{saved['id']}").json()

    assert requested_targets == ["zh"]
    assert detail["brief_items"] == brief_items
    assert detail["meaning"] == "想法；概念；构思"


def test_dictionary_brief_rejects_long_model_explanations_and_preserves_fallback_source(monkeypatch: pytest.MonkeyPatch):
    definitions = [
        {"part_of_speech": "noun", "text": "A thought or suggestion."},
        {"part_of_speech": "verb", "text": "To form a thought or plan."},
    ]
    translated_items = [
        {"part_of_speech": "noun", "text": definitions[0]["text"], "translation": "想法；建议", "provider": "baidu_api"},
        {"part_of_speech": "verb", "text": definitions[1]["text"], "translation": "构思", "provider": "baidu_api"},
    ]
    monkeypatch.setattr(
        server,
        "local_model_chat",
        lambda *args, **kwargs: '{"items":[{"sense_id":0,"gloss":"这是一个表示想法或建议的完整解释句"},{"sense_id":1,"gloss":"构思"}]}',
    )

    result = server.build_chinese_dictionary_brief_items(
        "idea", definitions, translated_items, True, "", ""
    )

    assert result == [
        {"part_of_speech": "noun", "text": "想法；建议", "provider": "baidu_api"},
        {"part_of_speech": "verb", "text": "构思", "provider": "baidu_api"},
    ]


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


def test_lookup_returns_context_meaning_and_dictionary_in_one_response(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    lesson = client.post("/api/lessons/demo").json()
    sentence = client.get(f"/api/lessons/{lesson['id']}").json()["sentences"][0]
    dictionary = {
        "word": "University", "source_language": "en", "target_language": "zh",
        "brief_items": [{"part_of_speech": "noun", "text": "大学", "provider": "local_model"}],
        "definitions": [{"part_of_speech": "noun", "text": "An institution of higher education."}],
        "phonetics": [], "example_items": [],
    }
    calls = []
    monkeypatch.setattr(server, "dictionary_payload_for_lookup", lambda word, source, target: (calls.append(word), dictionary)[1])
    monkeypatch.setattr(server, "translate_text", lambda *args, **kwargs: "大学")

    response = client.post("/api/lookup", json={
        "sentence_id": sentence["id"], "word": "University", "target_language": "zh", "save": False,
    })

    assert response.status_code == 200
    assert response.json()["meaning"] == "大学"
    assert response.json()["dictionary"] == dictionary
    assert calls == ["University"]


def test_saved_vocabulary_persists_complete_dictionary_snapshot(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    lesson = client.post("/api/lessons/demo").json()
    sentence = client.get(f"/api/lessons/{lesson['id']}").json()["sentences"][0]
    dictionary = {
        "word": "University", "source_language": "en", "target_language": "zh",
        "brief_items": [{"part_of_speech": "noun", "text": "大学", "provider": "local_model"}],
        "definitions": [{"part_of_speech": "noun", "text": "An institution of higher education."}],
        "phonetics": [{"region": "英式", "text": "/juː/", "audio": ""}],
        "example_items": [],
    }
    monkeypatch.setattr(server, "dictionary_payload_for_lookup", lambda word, source, target: dictionary)

    saved = client.post("/api/vocabulary", json={
        "sentence_id": sentence["id"], "word": "University", "target_language": "zh", "meaning": "大学",
    })
    assert saved.status_code == 200
    detail = client.get(f"/api/vocabulary/{saved.json()['id']}").json()
    assert detail["dictionary"] == dictionary
    assert detail["brief_items"] == dictionary["brief_items"]


def test_phrase_lookup_uses_sentence_context_and_caches(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    calls = []

    def fake_translate(phrase, source, target, context=None):
        calls.append((phrase, source, target, context))
        return f"{phrase}：{context}"

    monkeypatch.setattr(server, "translate_text", fake_translate)
    lesson = client.post("/api/lessons/demo").json()
    sentences = client.get(f"/api/lessons/{lesson['id']}").json()["sentences"]
    first, second = sentences[:2]
    client.patch(f"/api/sentences/{first['id']}", json={"text": "We get along with our neighbors.", "start_ms": 0, "end_ms": 4000})
    client.patch(f"/api/sentences/{second['id']}", json={"text": "They get along with the new team.", "start_ms": 4000, "end_ms": 8000})

    payload = {"sentence_id": first["id"], "phrase": " GET   along with ", "target_language": "zh"}
    first_result = client.post("/api/phrase-lookup", json=payload)
    cached_result = client.post("/api/phrase-lookup", json=payload)
    other_context = client.post("/api/phrase-lookup", json={**payload, "sentence_id": second["id"]})

    assert first_result.status_code == 200
    assert first_result.json()["meaning"] == "GET along with：We get along with our neighbors."
    assert first_result.json()["cached"] is False
    assert cached_result.json()["cached"] is True
    assert other_context.json()["meaning"] == "GET along with：They get along with the new team."
    assert len(calls) == 2
    assert client.post("/api/phrase-lookup", json={**payload, "phrase": "get"}).status_code == 400
    assert client.post("/api/phrase-lookup", json={**payload, "phrase": "by the way"}).status_code == 400
    assert client.post("/api/phrase-lookup", json={**payload, "phrase": "long with"}).status_code == 400


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
