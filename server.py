from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
UPLOAD_DIR = ROOT / "uploads"
STATIC_DIR = ROOT / "static"
DB_PATH = DATA_DIR / "study.sqlite3"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".webm"}
LANGUAGES = {"en": "English", "ja": "Japanese", "zh": "Chinese", "ko": "Korean", "fr": "French", "es": "Spanish"}
MODEL_LOCK = threading.Lock()
_whisper_model: Any = None

DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

app = FastAPI(title="言间 · 语言精听工作台", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@contextmanager
def db() -> Any:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize_database() -> None:
    with db() as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS lessons (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, source_language TEXT NOT NULL,
                audio_path TEXT, audio_name TEXT, duration_ms INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ready', created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sentences (
                id TEXT PRIMARY KEY, lesson_id TEXT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
                original_text TEXT NOT NULL, edited_text TEXT, confidence REAL,
                UNIQUE(lesson_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS translations (
                id TEXT PRIMARY KEY, sentence_id TEXT NOT NULL REFERENCES sentences(id) ON DELETE CASCADE,
                source_text TEXT NOT NULL, target_language TEXT NOT NULL, translated_text TEXT NOT NULL,
                created_at TEXT NOT NULL, UNIQUE(sentence_id, source_text, target_language)
            );
            CREATE TABLE IF NOT EXISTS vocabulary (
                id TEXT PRIMARY KEY, lesson_id TEXT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
                sentence_id TEXT NOT NULL REFERENCES sentences(id) ON DELETE CASCADE,
                word TEXT NOT NULL, source_language TEXT NOT NULL, target_language TEXT NOT NULL,
                meaning TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(sentence_id, word, target_language)
            );
            """
        )


initialize_database()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def sentence_text(row: sqlite3.Row) -> str:
    return row["edited_text"] or row["original_text"]


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?。！？])\s+|(?<=[。！？])(?=[^。！？])", text.strip())
    return [part.strip() for part in parts if part.strip()] or ([text.strip()] if text.strip() else [])


def split_with_times(text: str, start: float, end: float) -> list[tuple[str, int, int]]:
    parts = split_sentences(text)
    if not parts:
        return []
    weights = [max(1, len(re.sub(r"\s", "", part))) for part in parts]
    total = sum(weights)
    cursor = start
    result: list[tuple[str, int, int]] = []
    for index, (part, weight) in enumerate(zip(parts, weights)):
        part_end = end if index == len(parts) - 1 else cursor + (end - start) * weight / total
        result.append((part, max(0, round(cursor * 1000)), max(0, round(part_end * 1000))))
        cursor = part_end
    return result


def require_lesson(lesson_id: str) -> sqlite3.Row:
    with db() as conn:
        row = conn.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "找不到这份听力材料")
    return row


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    import importlib.util
    return {
        "ok": True,
        "transcription_available": importlib.util.find_spec("faster_whisper") is not None,
        "translation_available": bool(os.getenv("TRANSLATE_URL")),
    }


@app.get("/api/lessons")
def list_lessons() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT l.*, (SELECT COUNT(*) FROM sentences s WHERE s.lesson_id=l.id) AS sentence_count "
            "FROM lessons l ORDER BY created_at DESC"
        ).fetchall()
    return [row_dict(row) for row in rows]


@app.get("/api/vocabulary")
def list_vocabulary() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT v.*,l.title AS lesson_title,s.original_text FROM vocabulary v "
            "JOIN lessons l ON l.id=v.lesson_id JOIN sentences s ON s.id=v.sentence_id "
            "ORDER BY v.created_at DESC"
        ).fetchall()
    return [row_dict(row) for row in rows]


@app.get("/api/lessons/{lesson_id}")
def get_lesson(lesson_id: str) -> dict[str, Any]:
    lesson = require_lesson(lesson_id)
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM sentences WHERE lesson_id = ? ORDER BY sequence", (lesson_id,)
        ).fetchall()
        translations = conn.execute(
            "SELECT t.* FROM translations t JOIN sentences s ON s.id=t.sentence_id "
            "WHERE s.lesson_id=?", (lesson_id,)
        ).fetchall()
        words = conn.execute("SELECT * FROM vocabulary WHERE lesson_id=?", (lesson_id,)).fetchall()
    return {
        "lesson": row_dict(lesson),
        "sentences": [row_dict(row) for row in rows],
        "translations": [row_dict(row) for row in translations],
        "vocabulary": [row_dict(row) for row in words],
    }


@app.post("/api/lessons")
async def create_lesson(request: Request) -> dict[str, Any]:
    original_name = unquote(request.headers.get("x-file-name", "audio"))
    title = unquote(request.headers.get("x-title", ""))
    source_language = request.headers.get("x-source-language", "en")
    if source_language not in {"en", "ja"}:
        raise HTTPException(400, "第一版请选择英语或日语")
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, "不支持此音频格式，请上传 MP3、WAV、M4A、AAC、OGG、FLAC 或 WEBM")
    lesson_id = uuid.uuid4().hex
    safe_name = f"{lesson_id}{extension}"
    destination = UPLOAD_DIR / safe_name
    size = 0
    try:
        with destination.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "音频文件不能超过 200 MB")
                out.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    filename = Path(original_name or safe_name).name
    clean_title = (title or Path(filename).stem).strip()[:120] or "未命名听力"
    with db() as conn:
        conn.execute(
            "INSERT INTO lessons(id,title,source_language,audio_path,audio_name,status,created_at) "
            "VALUES(?,?,?,?,?,'uploaded',?)",
            (lesson_id, clean_title, source_language, safe_name, filename, now_iso()),
        )
    return {"id": lesson_id, "title": clean_title, "status": "uploaded", "audio_url": f"/api/lessons/{lesson_id}/audio"}


@app.get("/api/lessons/{lesson_id}/audio")
def audio(lesson_id: str) -> FileResponse:
    lesson = require_lesson(lesson_id)
    if not lesson["audio_path"]:
        raise HTTPException(404, "此示例材料没有音频")
    path = UPLOAD_DIR / lesson["audio_path"]
    if not path.is_file():
        raise HTTPException(404, "音频文件不存在")
    return FileResponse(path, filename=lesson["audio_name"] or path.name, content_disposition_type="inline")


def transcribe_lesson(lesson_id: str) -> None:
    global _whisper_model
    try:
        from faster_whisper import WhisperModel

        lesson = require_lesson(lesson_id)
        with MODEL_LOCK:
            if _whisper_model is None:
                _whisper_model = WhisperModel(
                    os.getenv("WHISPER_MODEL", "small"),
                    device=os.getenv("WHISPER_DEVICE", "cpu"),
                    compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
                )
        segments, _info = _whisper_model.transcribe(
            str(UPLOAD_DIR / lesson["audio_path"]),
            language=lesson["source_language"],
            vad_filter=True,
            word_timestamps=True,
        )
        materialized = list(segments)
        with db() as conn:
            conn.execute("DELETE FROM sentences WHERE lesson_id=?", (lesson_id,))
            sequence = 0
            for segment in materialized:
                for text, start_ms, end_ms in split_with_times(segment.text.strip(), segment.start, segment.end):
                    sequence += 1
                    conn.execute(
                        "INSERT INTO sentences(id,lesson_id,sequence,start_ms,end_ms,original_text,confidence) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, lesson_id, sequence, start_ms, max(start_ms + 1, end_ms), text, getattr(segment, "avg_logprob", None)),
                    )
            duration_ms = max((round(s.end * 1000) for s in materialized), default=0)
            conn.execute("UPDATE lessons SET status='ready',duration_ms=? WHERE id=?", (duration_ms, lesson_id))
    except Exception:
        with db() as conn:
            conn.execute("UPDATE lessons SET status='failed' WHERE id=?", (lesson_id,))
        raise


@app.post("/api/lessons/{lesson_id}/transcribe")
def start_transcription(lesson_id: str, background_tasks: BackgroundTasks) -> dict[str, str]:
    lesson = require_lesson(lesson_id)
    try:
        import importlib.util
        available = importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        available = False
    if not available:
        with db() as conn:
            conn.execute("UPDATE lessons SET status='uploaded' WHERE id=?", (lesson_id,))
        raise HTTPException(
            503,
            "本地语音识别尚未安装。请在项目终端运行：python -m pip install -r requirements-ml.txt；然后重新启动服务。音频已保存在本机。",
        )
    if lesson["status"] == "processing":
        return {"status": "processing"}
    with db() as conn:
        conn.execute("UPDATE lessons SET status='processing' WHERE id=?", (lesson_id,))
    background_tasks.add_task(transcribe_lesson, lesson_id)
    return {"status": "processing"}


class SentenceUpdate(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


class SplitRequest(BaseModel):
    offset: int = Field(gt=0)


@app.patch("/api/sentences/{sentence_id}")
def update_sentence(sentence_id: str, body: SentenceUpdate) -> dict[str, Any]:
    if body.end_ms <= body.start_ms:
        raise HTTPException(400, "结束时间必须晚于开始时间")
    with db() as conn:
        row = conn.execute("SELECT * FROM sentences WHERE id=?", (sentence_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "找不到这句话")
        conn.execute(
            "UPDATE sentences SET edited_text=?,start_ms=?,end_ms=? WHERE id=?",
            (body.text.strip(), body.start_ms, body.end_ms, sentence_id),
        )
        updated = conn.execute("SELECT * FROM sentences WHERE id=?", (sentence_id,)).fetchone()
    return row_dict(updated)


@app.post("/api/sentences/{sentence_id}/split")
def split_sentence(sentence_id: str, body: SplitRequest) -> dict[str, Any]:
    with db() as conn:
        sentence = conn.execute("SELECT * FROM sentences WHERE id=?", (sentence_id,)).fetchone()
        if sentence is None:
            raise HTTPException(404, "找不到这句话")
        text = sentence_text(sentence)
        if body.offset >= len(text):
            raise HTTPException(400, "请把光标放在句子中间再拆分")
        left = text[:body.offset].strip()
        right = text[body.offset:].strip()
        if not left or not right:
            raise HTTPException(400, "拆分后的两部分都需要有文字")
        midpoint = sentence["start_ms"] + round((sentence["end_ms"] - sentence["start_ms"]) * len(left) / len(text))
        following = conn.execute(
            "SELECT sequence FROM sentences WHERE lesson_id=? AND sequence>? ORDER BY sequence DESC",
            (sentence["lesson_id"], sentence["sequence"]),
        ).fetchall()
        for row in following:
            conn.execute("UPDATE sentences SET sequence=? WHERE lesson_id=? AND sequence=?", (row["sequence"] + 1, sentence["lesson_id"], row["sequence"]))
        conn.execute("UPDATE sentences SET edited_text=?,end_ms=? WHERE id=?", (left, midpoint, sentence_id))
        new_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO sentences(id,lesson_id,sequence,start_ms,end_ms,original_text,edited_text) VALUES(?,?,?,?,?,?,?)",
            (new_id, sentence["lesson_id"], sentence["sequence"] + 1, midpoint, sentence["end_ms"], right, right),
        )
        return {"id": new_id, "lesson_id": sentence["lesson_id"]}


@app.post("/api/sentences/{sentence_id}/merge-next")
def merge_next_sentence(sentence_id: str) -> dict[str, Any]:
    with db() as conn:
        sentence = conn.execute("SELECT * FROM sentences WHERE id=?", (sentence_id,)).fetchone()
        if sentence is None:
            raise HTTPException(404, "找不到这句话")
        next_sentence = conn.execute(
            "SELECT * FROM sentences WHERE lesson_id=? AND sequence=?",
            (sentence["lesson_id"], sentence["sequence"] + 1),
        ).fetchone()
        if next_sentence is None:
            raise HTTPException(400, "这已经是最后一句")
        combined = f"{sentence_text(sentence).rstrip()} {sentence_text(next_sentence).lstrip()}"
        conn.execute("UPDATE sentences SET edited_text=?,end_ms=? WHERE id=?", (combined, next_sentence["end_ms"], sentence_id))
        conn.execute("DELETE FROM sentences WHERE id=?", (next_sentence["id"],))
        following = conn.execute(
            "SELECT sequence FROM sentences WHERE lesson_id=? AND sequence>? ORDER BY sequence",
            (sentence["lesson_id"], sentence["sequence"] + 1),
        ).fetchall()
        for row in following:
            conn.execute("UPDATE sentences SET sequence=? WHERE lesson_id=? AND sequence=?", (row["sequence"] - 1, sentence["lesson_id"], row["sequence"]))
        return {"id": sentence_id, "lesson_id": sentence["lesson_id"]}


class TranslateRequest(BaseModel):
    target_language: str


def translate_text(text: str, source: str, target: str, context: str | None = None) -> str:
    base_url = os.getenv("TRANSLATE_URL", "").rstrip("/")
    if not base_url:
        raise HTTPException(503, "尚未配置翻译服务。请在 README 中配置 TRANSLATE_URL 后重启应用。")
    payload: dict[str, str] = {"q": text, "source": source, "target": target, "format": "text"}
    if context:
        payload["context"] = context
    api_key = os.getenv("TRANSLATE_API_KEY")
    if api_key:
        payload["api_key"] = api_key
    try:
        response = httpx.post(f"{base_url}/translate", data=payload, timeout=30)
        response.raise_for_status()
        translated = response.json().get("translatedText", "").strip()
        if not translated:
            raise ValueError("翻译服务没有返回译文")
        return translated
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(502, f"翻译服务暂不可用：{exc}") from exc


@app.post("/api/sentences/{sentence_id}/translate")
def translate_sentence(sentence_id: str, body: TranslateRequest) -> dict[str, Any]:
    if body.target_language not in LANGUAGES:
        raise HTTPException(400, "暂不支持该目标语言")
    with db() as conn:
        sentence = conn.execute(
            "SELECT s.*,l.source_language FROM sentences s JOIN lessons l ON l.id=s.lesson_id WHERE s.id=?",
            (sentence_id,),
        ).fetchone()
        if sentence is None:
            raise HTTPException(404, "找不到这句话")
        text = sentence_text(sentence)
        cached = conn.execute(
            "SELECT * FROM translations WHERE sentence_id=? AND source_text=? AND target_language=?",
            (sentence_id, text, body.target_language),
        ).fetchone()
        if cached:
            return row_dict(cached)
        source = sentence["source_language"]
    translated = translate_text(text, source, body.target_language)
    with db() as conn:
        translation_id = uuid.uuid4().hex
        conn.execute(
            "INSERT OR IGNORE INTO translations(id,sentence_id,source_text,target_language,translated_text,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (translation_id, sentence_id, text, body.target_language, translated, now_iso()),
        )
        row = conn.execute(
            "SELECT * FROM translations WHERE sentence_id=? AND source_text=? AND target_language=?",
            (sentence_id, text, body.target_language),
        ).fetchone()
    return row_dict(row)


class LookupRequest(BaseModel):
    sentence_id: str
    word: str = Field(min_length=1, max_length=120)
    target_language: str


@app.post("/api/lookup")
def lookup_word(body: LookupRequest) -> dict[str, Any]:
    if body.target_language not in LANGUAGES:
        raise HTTPException(400, "暂不支持该目标语言")
    with db() as conn:
        sentence = conn.execute(
            "SELECT s.*,l.source_language,l.id AS lesson_id FROM sentences s JOIN lessons l ON l.id=s.lesson_id WHERE s.id=?",
            (body.sentence_id,),
        ).fetchone()
        if sentence is None:
            raise HTTPException(404, "找不到这句话")
        source = sentence["source_language"]
        context = sentence_text(sentence)
        saved = conn.execute(
            "SELECT * FROM vocabulary WHERE sentence_id=? AND word=? AND target_language=?",
            (body.sentence_id, body.word, body.target_language),
        ).fetchone()
        if saved:
            return row_dict(saved)
    meaning = translate_text(body.word, source, body.target_language, context)
    vocabulary_id = saved["id"] if saved else uuid.uuid4().hex
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO vocabulary(id,lesson_id,sentence_id,word,source_language,target_language,meaning,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (vocabulary_id, sentence["lesson_id"], body.sentence_id, body.word, source, body.target_language, meaning, now_iso()),
        )
        row = conn.execute(
            "SELECT * FROM vocabulary WHERE sentence_id=? AND word=? AND target_language=?",
            (body.sentence_id, body.word, body.target_language),
        ).fetchone()
    return row_dict(row)


@app.post("/api/lessons/demo")
def create_demo() -> dict[str, Any]:
    lesson_id = uuid.uuid4().hex
    examples = [
        ("I used to think that learning a language meant memorizing as many words as possible.", "我过去以为，学习一门语言就是尽可能多地记单词。", 0, 5200),
        ("But the more I listened, the more I noticed how people connect ideas in conversation.", "但我听得越多，就越能注意到人们在交谈中如何串联想法。", 5500, 10800),
        ("Now I practice with short recordings and repeat each sentence until it feels natural.", "现在我用短录音练习，反复跟读每句话，直到说起来自然。", 11200, 16800),
        ("A few focused minutes every day can make a real difference over time.", "每天专注练习几分钟，积累一段时间后就会有明显进步。", 17200, 21600),
    ]
    with db() as conn:
        conn.execute(
            "INSERT INTO lessons(id,title,source_language,status,created_at) VALUES(?,?,'en','ready',?)",
            (lesson_id, "每日精听 · 示例课程", now_iso()),
        )
        for sequence, (text, translation, start_ms, end_ms) in enumerate(examples, 1):
            sentence_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO sentences(id,lesson_id,sequence,start_ms,end_ms,original_text) VALUES(?,?,?,?,?,?)",
                (sentence_id, lesson_id, sequence, start_ms, end_ms, text),
            )
            conn.execute(
                "INSERT INTO translations(id,sentence_id,source_text,target_language,translated_text,created_at) VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, sentence_id, text, "zh", translation, now_iso()),
            )
    return {"id": lesson_id, "title": "每日精听 · 示例课程", "status": "ready"}
