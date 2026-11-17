from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import sqlite3
import threading
import uuid
import gc
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
# Windows 下 ctranslate2 与其他科学计算依赖可能各自携带 OpenMP 运行库。
# 允许它们共存，避免后台转写线程加载模型时直接终止整个服务进程。
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
DATA_DIR = ROOT / "data"
UPLOAD_DIR = ROOT / "uploads"
WHISPER_MODELS_DIR = ROOT / "models" / "whisper"
STATIC_DIR = ROOT / "static"
DB_PATH = DATA_DIR / "study.sqlite3"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".webm"}
LANGUAGES = {"en": "English", "ja": "Japanese", "zh": "Chinese", "ko": "Korean", "fr": "French", "es": "Spanish"}
LOCAL_WORD_GLOSSARY = {
    ("en", "zh", "i"): "我",
    ("en", "zh", "used"): "过去常常",
    ("en", "zh", "think"): "认为；想",
    ("en", "zh", "learning"): "学习",
    ("en", "zh", "language"): "语言",
    ("en", "zh", "meant"): "意味着",
    ("en", "zh", "memorizing"): "记忆；背诵",
    ("en", "zh", "words"): "单词",
    ("en", "zh", "listened"): "听；听过",
    ("en", "zh", "noticed"): "注意到",
    ("en", "zh", "connect"): "连接；串联",
    ("en", "zh", "ideas"): "想法",
    ("en", "zh", "conversation"): "交谈；对话",
    ("en", "zh", "practice"): "练习",
    ("en", "zh", "recordings"): "录音",
    ("en", "zh", "repeat"): "重复；跟读",
    ("en", "zh", "sentence"): "句子",
    ("en", "zh", "natural"): "自然的",
    ("en", "zh", "focused"): "专注的",
    ("en", "zh", "minutes"): "分钟",
    ("en", "zh", "every"): "每个；每隔",
    ("en", "zh", "day"): "天",
    ("en", "zh", "difference"): "差别；进步",
    ("en", "zh", "time"): "时间",
}
MODEL_LOCK = threading.RLock()
JOB_LOCK = threading.RLock()
_whisper_model: Any = None
_whisper_model_name: str | None = None
_active_transcriptions: dict[str, threading.Event] = {}
_pending_model: str | None = None
_model_switching = False
_model_switch_error: str | None = None
_selected_model = os.getenv("WHISPER_MODEL", "tiny") if os.getenv("WHISPER_MODEL", "tiny") in {"tiny", "base", "small"} else "tiny"
MODEL_OPTIONS = {"tiny": "快速", "base": "平衡", "small": "精准"}
CURATED_DICTIONARY_EXAMPLES = {
    "i": {
        "noun": "The word ‘I’ is always written with a capital letter.",
        "pronoun": "I am learning English.",
    }
}

DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

app = FastAPI(title="言间 · 语言精听工作台", version="0.1.0")
logger = logging.getLogger(__name__)
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
                status TEXT NOT NULL DEFAULT 'ready', progress INTEGER NOT NULL DEFAULT 0,
                progress_message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
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
        columns = {row[1] for row in conn.execute("PRAGMA table_info(lessons)").fetchall()}
        if "progress" not in columns:
            conn.execute("ALTER TABLE lessons ADD COLUMN progress INTEGER NOT NULL DEFAULT 0")
        if "progress_message" not in columns:
            conn.execute("ALTER TABLE lessons ADD COLUMN progress_message TEXT NOT NULL DEFAULT ''")


initialize_database()


@app.on_event("startup")
def recover_interrupted_transcriptions() -> None:
    with db() as conn:
        conn.execute("UPDATE lessons SET status='uploaded',progress=0,progress_message=? WHERE status='processing'", ("识别因服务重启而中断，请重新开始",))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def sentence_text(row: sqlite3.Row) -> str:
    return row["edited_text"] or row["original_text"]


def normalize_word(value: str) -> str:
    return "".join(char for char in value.strip().lower() if char.isalnum())


def find_saved_word(conn: sqlite3.Connection, source: str, target: str, word: str) -> sqlite3.Row | None:
    normalized = normalize_word(word)
    rows = conn.execute(
        "SELECT * FROM vocabulary WHERE source_language=? AND target_language=? ORDER BY created_at DESC",
        (source, target),
    ).fetchall()
    return next((row for row in rows if normalize_word(row["word"]) == normalized), None)


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
        "translation_available": bool((os.getenv("BAIDU_TRANSLATE_APP_ID") and os.getenv("BAIDU_TRANSLATE_SECRET")) or os.getenv("TRANSLATE_URL")),
    }


def model_is_available(model_name: str) -> bool:
    return (WHISPER_MODELS_DIR / model_name / "model.bin").is_file()


def recognition_state() -> dict[str, Any]:
    with JOB_LOCK:
        active = list(_active_transcriptions)
        pending = _pending_model
        switching = _model_switching
        switch_error = _model_switch_error
        selected = _selected_model
    with MODEL_LOCK:
        loaded = _whisper_model_name
    return {
        "selected_model": selected,
        "loaded_model": loaded,
        "pending_model": pending,
        "switching": switching,
        "switch_error": switch_error,
        "active_jobs": active,
        "models": [
            {"id": key, "label": label, "available": model_is_available(key)}
            for key, label in MODEL_OPTIONS.items()
        ],
    }


@app.get("/api/recognition")
def get_recognition_settings() -> dict[str, Any]:
    return recognition_state()


class ModelSwitchRequest(BaseModel):
    model: str
    behavior: str = "immediate"


def load_whisper_model(model_name: str) -> None:
    global _whisper_model, _whisper_model_name, _selected_model
    from faster_whisper import WhisperModel

    model_path = WHISPER_MODELS_DIR / model_name
    if not (model_path / "model.bin").is_file():
        raise HTTPException(409, f"{model_name} 模型文件尚未完整下载")
    with MODEL_LOCK:
        if _whisper_model_name == model_name and _whisper_model is not None:
            _selected_model = model_name
            return
        _whisper_model = None
        _whisper_model_name = None
        gc.collect()
        _whisper_model = WhisperModel(
            str(model_path),
            device=os.getenv("WHISPER_DEVICE", "cpu"),
            compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
        )
        _whisper_model_name = model_name
        _selected_model = model_name


def complete_pending_model_switch() -> None:
    global _pending_model, _model_switching, _model_switch_error
    with JOB_LOCK:
        if _active_transcriptions or not _pending_model or _model_switching:
            return
        target = _pending_model
        _pending_model = None
        _model_switching = True
    try:
        load_whisper_model(target)
    except Exception:
        logger.exception("Deferred Whisper model switch to %s failed", target)
        with JOB_LOCK:
            _pending_model = None
            _model_switch_error = f"切换到 {MODEL_OPTIONS[target]} 模式失败"
    finally:
        with JOB_LOCK:
            _model_switching = False


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
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (row["source_language"], row["target_language"], normalize_word(row["word"]))
        if key in seen:
            continue
        seen.add(key)
        result.append(row_dict(row))
    return result


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


class LessonRename(BaseModel):
    title: str = Field(min_length=1, max_length=120)


@app.patch("/api/lessons/{lesson_id}")
def rename_lesson(lesson_id: str, body: LessonRename) -> dict[str, Any]:
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "材料名称不能为空")
    with db() as conn:
        cursor = conn.execute("UPDATE lessons SET title=? WHERE id=?", (title, lesson_id))
        if cursor.rowcount == 0:
            raise HTTPException(404, "找不到这份听力材料")
        row = conn.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)).fetchone()
    return row_dict(row)


@app.delete("/api/lessons/{lesson_id}")
def delete_lesson(lesson_id: str) -> dict[str, str]:
    with db() as conn:
        lesson = conn.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)).fetchone()
        if lesson is None:
            raise HTTPException(404, "找不到这份听力材料")
        conn.execute("DELETE FROM lessons WHERE id=?", (lesson_id,))
    if lesson["audio_path"]:
        (UPLOAD_DIR / Path(lesson["audio_path"]).name).unlink(missing_ok=True)
    return {"id": lesson_id, "status": "deleted"}


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


def set_lesson_progress(lesson_id: str, progress: int, message: str, status: str | None = None) -> None:
    with db() as conn:
        if status is None:
            conn.execute(
                "UPDATE lessons SET progress=?,progress_message=? WHERE id=?",
                (max(0, min(100, progress)), message, lesson_id),
            )
        else:
            conn.execute(
                "UPDATE lessons SET status=?,progress=?,progress_message=? WHERE id=?",
                (status, max(0, min(100, progress)), message, lesson_id),
            )


def transcribe_lesson(lesson_id: str, cancel_event: threading.Event) -> None:
    model = None
    segments = None
    _info = None
    try:
        lesson = require_lesson(lesson_id)
        set_lesson_progress(lesson_id, 5, "正在加载语音识别模型…", "processing")
        if cancel_event.is_set():
            set_lesson_progress(lesson_id, 0, "已取消识别，准备切换模型", "uploaded")
            return
        model_name = _selected_model
        if not model_is_available(model_name):
            raise RuntimeError(f"本地缺少 {model_name} 模型，请先下载模型文件")
        with MODEL_LOCK:
            model = _whisper_model if _whisper_model_name == model_name else None
        if model is None:
            load_whisper_model(model_name)
            with MODEL_LOCK:
                model = _whisper_model
        if cancel_event.is_set():
            set_lesson_progress(lesson_id, 0, "已取消识别，准备切换模型", "uploaded")
            return
        set_lesson_progress(lesson_id, 18, "模型已加载，正在分析音频…")
        segments, _info = model.transcribe(
            str(UPLOAD_DIR / lesson["audio_path"]),
            language=lesson["source_language"],
            vad_filter=True,
            word_timestamps=True,
        )
        materialized = []
        duration_hint = float(getattr(_info, "duration", 0) or 0)
        for segment in segments:
            if cancel_event.is_set():
                set_lesson_progress(lesson_id, 0, "已取消识别，准备切换模型", "uploaded")
                return
            materialized.append(segment)
            if duration_hint > 0:
                progress = min(88, 18 + round((float(segment.end) / duration_hint) * 68))
            else:
                progress = min(88, 18 + len(materialized))
            if len(materialized) == 1 or len(materialized) % 3 == 0:
                set_lesson_progress(lesson_id, progress, f"已识别 {len(materialized)} 个语音片段…")
        if cancel_event.is_set():
            set_lesson_progress(lesson_id, 0, "已取消识别，准备切换模型", "uploaded")
            return
        set_lesson_progress(lesson_id, 92, "正在生成逐句听力稿…")
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
            conn.execute(
                "UPDATE lessons SET status='ready',duration_ms=?,progress=100,progress_message=? WHERE id=?",
                (duration_ms, "识别完成", lesson_id),
            )
    except Exception as exc:
        if "metadata_errors" in str(exc) and "unexpected keyword argument" in str(exc):
            message = "语音解码依赖不兼容：请在项目环境执行 `python -m pip install -r requirements-ml.txt`，然后重启服务"
        else:
            message = f"识别失败：{exc}"
        with db() as conn:
            conn.execute(
                "UPDATE lessons SET status='failed',progress_message=? WHERE id=?",
                (message, lesson_id),
            )
        raise
    finally:
        segments = None
        model = None
        _info = None
        gc.collect()
        with JOB_LOCK:
            _active_transcriptions.pop(lesson_id, None)
        complete_pending_model_switch()


@app.post("/api/lessons/{lesson_id}/transcribe")
def start_transcription(lesson_id: str, force: bool = False) -> dict[str, str]:
    lesson = require_lesson(lesson_id)
    try:
        import importlib.util
        available = importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        available = False
    if not available:
        with db() as conn:
            conn.execute(
                "UPDATE lessons SET status='uploaded',progress=0,progress_message=? WHERE id=?",
                ("尚未安装 faster-whisper，请先安装本地识别模型", lesson_id),
            )
        raise HTTPException(
            503,
            "本地语音识别尚未安装。请在项目终端运行：python -m pip install -r requirements-ml.txt；然后重新启动服务。音频已保存在本机。",
        )
    with JOB_LOCK:
        if _model_switching or _pending_model:
            raise HTTPException(409, "语音模型正在切换，请稍后再开始识别")
        if (lesson["status"] == "processing" or lesson_id in _active_transcriptions) and not force:
            return {"status": "processing"}
        cancel_event = threading.Event()
        _active_transcriptions[lesson_id] = cancel_event
    with db() as conn:
        conn.execute(
            "UPDATE lessons SET status='processing',progress=1,progress_message=? WHERE id=?",
            ("任务已排队，准备开始识别…", lesson_id),
        )
    threading.Thread(target=transcribe_lesson, args=(lesson_id, cancel_event), daemon=True).start()
    return {"status": "processing"}


@app.post("/api/lessons/{lesson_id}/reparse")
def reparse_lesson(lesson_id: str) -> dict[str, str]:
    """重新使用当前选择的 Whisper 模型解析材料。"""
    lesson = require_lesson(lesson_id)
    with JOB_LOCK:
        if lesson_id in _active_transcriptions or lesson["status"] == "processing":
            raise HTTPException(409, "这份材料正在解析，请等待当前任务完成")
    # 保留旧句子直到新解析完成，避免后台任务期间页面突然变空。
    with db() as conn:
        conn.execute(
            "UPDATE lessons SET status='uploaded',progress=0,progress_message=? WHERE id=?",
            (f"准备使用当前识别模式重新解析（{_selected_model}）…", lesson_id),
        )
    return start_transcription(lesson_id, force=True)


@app.post("/api/recognition/model")
def switch_recognition_model(body: ModelSwitchRequest) -> dict[str, Any]:
    global _pending_model, _model_switching, _model_switch_error
    if body.model not in MODEL_OPTIONS:
        raise HTTPException(400, "未知的语音识别模型")
    if body.behavior not in {"immediate", "wait", "cancel"}:
        raise HTTPException(400, "无效的并发处理方式")
    if not model_is_available(body.model):
        raise HTTPException(409, f"{body.model} 模型尚未下载完成")
    with JOB_LOCK:
        if _model_switching:
            raise HTTPException(409, "另一个模型切换正在进行")
        _model_switch_error = None
        active = list(_active_transcriptions.items())
        if active and body.behavior == "immediate":
            raise HTTPException(409, "有转写任务正在运行，请选择等待或取消后切换")
        if active and body.behavior == "wait":
            _pending_model = body.model
            return {**recognition_state(), "message": "当前转写结束后会自动切换模型"}
        if active and body.behavior == "cancel":
            _pending_model = body.model
            for _lesson_id, event in active:
                event.set()
            return {**recognition_state(), "message": "正在停止当前转写，完成后自动切换模型"}
        _model_switching = True
    try:
        load_whisper_model(body.model)
    finally:
        with JOB_LOCK:
            _model_switching = False
    return {**recognition_state(), "message": f"已切换到{MODEL_OPTIONS[body.model]}模式"}


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
    force: bool = False


class DictionaryRequest(BaseModel):
    word: str = Field(min_length=1, max_length=120)
    source_language: str
    target_language: str


def dictionary_example_is_usable(example: str, clean_word: str) -> bool:
    """Keep complete, sentence-like examples that contain the queried word."""
    text = re.sub(r"\s+", " ", str(example or "")).strip()
    if len(text) < 8 or not text[0].isupper():
        return False
    forms = [clean_word]
    if clean_word.casefold().endswith("ies") and len(clean_word) > 4:
        forms.append(clean_word[:-3] + "y")
    elif clean_word.casefold().endswith("s") and len(clean_word) > 3:
        forms.append(clean_word[:-1])
    if not any(re.search(rf"(?<![\w'-]){re.escape(form)}(?![\w'-])", text, re.IGNORECASE) for form in forms):
        return False
    # This frequently returned dictionary sentence teaches an incorrect object form.
    if clean_word.casefold() == "i" and re.search(r"\band\s+i\b", text, re.IGNORECASE):
        return False
    return True


@app.post("/api/dictionary")
def dictionary_word(body: DictionaryRequest) -> dict[str, Any]:
    if body.source_language not in LANGUAGES or body.target_language not in LANGUAGES:
        raise HTTPException(400, "暂不支持该语言")
    if body.source_language != "en":
        raise HTTPException(503, "当前独立词典卡片先支持英语，日语词典接口正在准备中。")
    clean_word = body.word.strip().strip(".,!?;:()[]{}")
    if not clean_word:
        raise HTTPException(400, "单词不能为空")
    try:
        entries = None
        for attempt in range(2):
            try:
                response = httpx.get(
                    f"https://api.dictionaryapi.dev/api/v2/entries/en/{quote(clean_word)}",
                    timeout=15,
                )
                response.raise_for_status()
                entries = response.json()
                break
            except httpx.HTTPError:
                if attempt == 1:
                    raise
        if entries is None:
            raise ValueError("词典没有返回数据")
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Dictionary lookup failed (%s)", type(exc).__name__)
        raise HTTPException(502, "英文词典服务暂不可用，请稍后重试。") from exc
    if not isinstance(entries, list) or not entries:
        raise HTTPException(404, "词典中没有找到这个词")
    phonetics: list[dict[str, str]] = []
    definitions: list[dict[str, str]] = []
    sense_examples: list[dict[str, Any]] = []
    seen_phonetics: set[tuple[str, str]] = set()
    seen_definitions: set[tuple[str, str]] = set()
    for entry in entries:
        primary_ipa = str(entry.get("phonetic") or "").strip()
        if primary_ipa:
            key = ("发音", primary_ipa)
            if key not in seen_phonetics:
                seen_phonetics.add(key)
                phonetics.append({"region": "通用", "text": primary_ipa, "audio": ""})
        for phonetic in entry.get("phonetics", []):
            ipa = str(phonetic.get("text") or "").strip()
            audio = str(phonetic.get("audio") or "").strip()
            if not ipa and not audio:
                continue
            audio_name = audio.lower()
            region = "美式" if re.search(r"(?:^|[-_])us(?:[._-]|$)", audio_name) else "英式" if re.search(r"(?:^|[-_])(?:uk|gb)(?:[._-]|$)", audio_name) else "通用"
            key = (region, ipa or audio)
            if key not in seen_phonetics:
                seen_phonetics.add(key)
                phonetics.append({"region": region, "text": ipa, "audio": audio})
        for meaning_group in entry.get("meanings", []):
            part_of_speech = str(meaning_group.get("partOfSpeech") or "").strip()
            for definition in meaning_group.get("definitions", []):
                text = str(definition.get("definition") or "").strip()
                example = str(definition.get("example") or "").strip()
                if not text:
                    continue
                key = (part_of_speech, text)
                if key in seen_definitions:
                    continue
                seen_definitions.add(key)
                definition_index = len(definitions)
                definitions.append({"part_of_speech": part_of_speech, "text": text})
                if example:
                    sense_examples.append({"text": example, "sense_index": definition_index})
    if not definitions:
        raise HTTPException(404, "词典中没有找到可用释义")
    # 有些词典把通用 IPA 放在 entry.phonetic，把地区音频放在 phonetics。
    # 在地区条目缺少 IPA 时复用通用 IPA，并移除重复的通用展示。
    generic_ipas = [item["text"] for item in phonetics if item["region"] == "通用" and item["text"]]
    regional_items = [item for item in phonetics if item["region"] in {"美式", "英式"}]
    if generic_ipas and regional_items:
        for item in regional_items:
            if not item["text"]:
                item["text"] = generic_ipas[0]
        phonetics = regional_items + [item for item in phonetics if item["region"] == "通用" and item["text"] not in generic_ipas]
    # Free Dictionary 的返回顺序已经按词典常用度排列。不要用 Datamuse 的单条
    # 简释覆盖它，否则会丢失多词义和每个词义对应的例句。
    pos_names = {"noun": "n.", "verb": "v.", "adjective": "adj.", "adverb": "adv.", "preposition": "prep.", "pronoun": "pron."}
    english_meaning = "；".join(item["text"] for item in definitions)
    part_of_speech = definitions[0]["part_of_speech"]
    try:
        translated_meaning = english_meaning if body.target_language == "en" else translate_text(
            english_meaning, "en", body.target_language
        )
        meaning_translated = True
    except HTTPException:
        translated_meaning = english_meaning
        meaning_translated = body.target_language == "en"
    definition_items: list[dict[str, Any]] = []
    for definition in definitions:
        definition_translation = definition["text"] if body.target_language == "en" else ""
        if body.target_language != "en":
            try:
                definition_translation = translate_text(definition["text"], "en", body.target_language)
            except HTTPException:
                definition_translation = ""
        definition_items.append({
            "part_of_speech": definition["part_of_speech"],
            "text": definition["text"],
            "translation": definition_translation,
        })
    # 例句只取前三个有例句的不同词义；definitions 本身保留全部去重后的词义。
    selected_examples: list[dict[str, Any]] = []
    seen_example_senses: set[int] = set()
    for item in sense_examples:
        sense_index = int(item["sense_index"])
        if sense_index in seen_example_senses:
            continue
        if not dictionary_example_is_usable(item["text"], clean_word):
            continue
        seen_example_senses.add(sense_index)
        selected_examples.append(item)
        if len(selected_examples) >= 3:
            break
    # 词典没有给出可用例句时，使用经过整理的常见词例句，避免跨词义借用例句。
    curated = CURATED_DICTIONARY_EXAMPLES.get(clean_word.casefold(), {})
    for sense_index, sense in enumerate(definitions):
        if len(selected_examples) >= 3 or sense_index in seen_example_senses:
            continue
        fallback = curated.get(sense["part_of_speech"].casefold())
        if not fallback or not dictionary_example_is_usable(fallback, clean_word):
            continue
        seen_example_senses.add(sense_index)
        selected_examples.append({"text": fallback, "sense_index": sense_index})
    example_items: list[dict[str, Any]] = []
    for item in selected_examples:
        translated_example = ""
        if body.target_language == "en":
            translated_example = item["text"]
        else:
            try:
                translated_example = translate_text(item["text"], "en", body.target_language)
            except HTTPException:
                translated_example = ""
        sense = definitions[item["sense_index"]]
        example_items.append({"text": item["text"], "translation": translated_example, "sense_index": item["sense_index"], "sense": sense["text"], "part_of_speech": sense["part_of_speech"]})
    word_translation = clean_word
    if body.target_language != "en":
        try:
            word_translation = translate_text(clean_word, "en", body.target_language)
        except HTTPException:
            word_translation = ""
    return {
        "word": clean_word,
        "source_language": body.source_language,
        "target_language": body.target_language,
        "phonetics": phonetics[:8],
        "definitions": definitions,
        "definition_items": definition_items,
        "examples": [item["text"] for item in example_items],
        "example_items": example_items,
        "word_translation": word_translation,
        "meaning": translated_meaning,
        "part_of_speech_label": pos_names.get(part_of_speech, part_of_speech),
        "meaning_translated": meaning_translated,
    }


def translate_text(text: str, source: str, target: str, context: str | None = None) -> str:
    app_id = os.getenv("BAIDU_TRANSLATE_APP_ID", "").strip()
    secret = os.getenv("BAIDU_TRANSLATE_SECRET", "").strip()
    if app_id or secret:
        if not app_id or not secret:
            raise HTTPException(503, "百度翻译配置不完整，请同时填写 App ID 和密钥。")
        languages = {"en": "en", "ja": "jp", "zh": "zh", "ko": "kor", "fr": "fra", "es": "spa"}
        if source not in languages or target not in languages:
            raise HTTPException(400, "百度翻译暂不支持所选语言")
        if source == target:
            return text
        salt = uuid.uuid4().hex
        sign = hashlib.md5((app_id + text + salt + secret).encode("utf-8")).hexdigest()
        try:
            response = httpx.post(
                "https://fanyi-api.baidu.com/api/trans/vip/translate",
                data={"q": text, "from": languages[source], "to": languages[target], "appid": app_id, "salt": salt, "sign": sign},
                timeout=30,
            )
            response.raise_for_status()
            result = response.json()
            if result.get("error_code"):
                raise HTTPException(502, f"百度翻译请求失败（错误码 {result['error_code']}），请检查应用凭据、额度及服务状态。")
            translations = result.get("trans_result")
            if not isinstance(translations, list) or not translations:
                raise ValueError("百度翻译没有返回译文")
            translated = "\n".join(item.get("dst", "") for item in translations).strip()
            if not translated:
                raise ValueError("百度翻译没有返回译文")
            return translated
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(502, "百度翻译服务暂不可用，请检查网络或稍后重试。") from exc
    base_url = os.getenv("TRANSLATE_URL", "").rstrip("/")
    if not base_url:
        if context:
            normalized = text.strip().lower().strip(".,!?;:()[]{}")
            key = (source, target, normalized)
            return LOCAL_WORD_GLOSSARY.get(key, f"待配置翻译：{text}")
        raise HTTPException(503, "尚未配置百度翻译，请按 README 填写 App ID 和密钥并重启应用。")
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
        if cached and not body.force:
            return row_dict(cached)
        source = sentence["source_language"]
    translated = translate_text(text, source, body.target_language)
    with db() as conn:
        translation_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO translations(id,sentence_id,source_text,target_language,translated_text,created_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(sentence_id,source_text,target_language) DO UPDATE SET translated_text=excluded.translated_text,created_at=excluded.created_at",
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
    save: bool = True


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
        saved = find_saved_word(conn, source, body.target_language, body.word)
        if saved:
            return {**row_dict(saved), "saved": True}
    meaning = translate_text(body.word, source, body.target_language, context)
    if not body.save:
        return {
            "sentence_id": body.sentence_id,
            "word": body.word,
            "source_language": source,
            "target_language": body.target_language,
            "meaning": meaning,
            "saved": False,
        }
    vocabulary_id = uuid.uuid4().hex
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
    return {**row_dict(row), "saved": True}


class VocabularySaveRequest(LookupRequest):
    meaning: str = Field(min_length=1, max_length=4000)


@app.post("/api/vocabulary")
def save_vocabulary(body: VocabularySaveRequest) -> dict[str, Any]:
    if body.target_language not in LANGUAGES:
        raise HTTPException(400, "暂不支持该目标语言")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        sentence = conn.execute(
            "SELECT s.id,s.lesson_id,l.source_language FROM sentences s JOIN lessons l ON l.id=s.lesson_id WHERE s.id=?",
            (body.sentence_id,),
        ).fetchone()
        if sentence is None:
            raise HTTPException(404, "找不到这句话")
        saved = find_saved_word(conn, sentence["source_language"], body.target_language, body.word)
        if saved:
            return {**row_dict(saved), "saved": True}
        conn.execute(
            "INSERT OR IGNORE INTO vocabulary(id,lesson_id,sentence_id,word,source_language,target_language,meaning,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, sentence["lesson_id"], body.sentence_id, body.word, sentence["source_language"], body.target_language, body.meaning, now_iso()),
        )
        row = conn.execute(
            "SELECT * FROM vocabulary WHERE sentence_id=? AND word=? AND target_language=?",
            (body.sentence_id, body.word, body.target_language),
        ).fetchone()
    return {**row_dict(row), "saved": True}


@app.delete("/api/vocabulary/{vocabulary_id}")
def remove_vocabulary(vocabulary_id: str) -> dict[str, str]:
    with db() as conn:
        row = conn.execute("SELECT * FROM vocabulary WHERE id=?", (vocabulary_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "找不到这条收藏")
        matches = conn.execute("SELECT id,word FROM vocabulary WHERE source_language=? AND target_language=?", (row["source_language"], row["target_language"])).fetchall()
        conn.executemany("DELETE FROM vocabulary WHERE id=?", [(item["id"],) for item in matches if normalize_word(item["word"]) == normalize_word(row["word"])])
    return {"id": vocabulary_id, "status": "removed"}


@app.get("/api/vocabulary/{vocabulary_id}")
def get_vocabulary_detail(vocabulary_id: str) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute(
            "SELECT v.*,l.title AS lesson_title,s.original_text,s.edited_text,s.start_ms,s.end_ms "
            "FROM vocabulary v JOIN lessons l ON l.id=v.lesson_id JOIN sentences s ON s.id=v.sentence_id WHERE v.id=?",
            (vocabulary_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "找不到这条收藏")
        translations = conn.execute(
            "SELECT * FROM translations WHERE sentence_id=? ORDER BY created_at DESC",
            (row["sentence_id"],),
        ).fetchall()
    return {**row_dict(row), "sentence_text": row["edited_text"] or row["original_text"], "translations": [row_dict(item) for item in translations]}


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
