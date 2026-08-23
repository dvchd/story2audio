import os
import io
import re
import sys
import json
import time
import logging
import hashlib
import asyncio
import unicodedata
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import List, Dict, AsyncGenerator, Optional, Tuple

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import edge_tts
from gtts import gTTS
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Windows asyncio patch (suppress benign socket shutdown errors)
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    import asyncio.proactor_events

    _original_call_connection_lost = (
        asyncio.proactor_events._ProactorBasePipeTransport._call_connection_lost
    )

    def _patched_call_connection_lost(self, exc):
        try:
            _original_call_connection_lost(self, exc)
        except OSError:
            pass

    asyncio.proactor_events._ProactorBasePipeTransport._call_connection_lost = (
        _patched_call_connection_lost
    )

# ---------------------------------------------------------------------------
# Directories & Setup
# ---------------------------------------------------------------------------
VERSION = os.environ.get("APP_VERSION", "v3.2.0")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
CACHE_DIR = os.path.join(BASE_DIR, "audio_cache")

os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Dọn cache một lần lúc khởi động (nếu vượt MAX_CACHE_MB từ lần chạy trước)
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            prune_cache_dir,
            CACHE_DIR,
            MAX_CACHE_MB * 1024 * 1024,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("Prune cache lúc khởi động thất bại: %s", exc)
    yield


app = FastAPI(title="Story to Audio + Live Subtitles API", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

logger = logging.getLogger("story2audio")

# ---------------------------------------------------------------------------
# Cache-ID validation (chặn path traversal)
# ---------------------------------------------------------------------------
_CACHE_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def validate_cache_id(cache_id: str) -> str:
    cache_id = (cache_id or "").strip()
    if not _CACHE_ID_RE.fullmatch(cache_id):
        raise HTTPException(status_code=400, detail="Invalid cache id")
    return cache_id


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PROXY = os.getenv("PROXY")
ENABLE_DEBUG_TTS = os.getenv("ENABLE_DEBUG_TTS", "").lower() in {"1", "true", "yes"}

if PROXY:
    os.environ["HTTP_PROXY"] = PROXY
    os.environ["HTTPS_PROXY"] = PROXY

generation_status: Dict[str, dict] = {}
_generation_locks: Dict[str, asyncio.Lock] = {}

# ---------------------------------------------------------------------------
# Language & Voice Registry
# ---------------------------------------------------------------------------
EDGE_VOICES: Dict[str, List[Dict[str, str]]] = {
    "vi": [
        {"value": "vi-VN-HoaiMyNeural", "label": "Hoài Mỹ (Nữ Miền Nam)"},
        {"value": "vi-VN-NamMinhNeural", "label": "Nam Minh (Nam Miền Nam)"},
    ],
    "en": [
        {"value": "en-US-AriaNeural", "label": "Aria (Female, US)"},
        {"value": "en-US-GuyNeural", "label": "Guy (Male, US)"},
        {"value": "en-US-JennyNeural", "label": "Jenny (Female, US)"},
        {"value": "en-GB-SoniaNeural", "label": "Sonia (Female, UK)"},
        {"value": "en-GB-RyanNeural", "label": "Ryan (Male, UK)"},
        {"value": "en-AU-NatashaNeural", "label": "Natasha (Female, AU)"},
    ],
    "ja": [
        {"value": "ja-JP-NanamiNeural", "label": "Nanami (女性)"},
        {"value": "ja-JP-KeitaNeural", "label": "Keita (男性)"},
    ],
    "zh": [
        {"value": "zh-CN-XiaoxiaoNeural", "label": "晓晓 (女, 普通话)"},
        {"value": "zh-CN-YunxiNeural", "label": "云希 (男, 普通话)"},
        {"value": "zh-TW-HsiaoChenNeural", "label": "曉臻 (女, 台灣)"},
    ],
    "ko": [
        {"value": "ko-KR-SunHiNeural", "label": "선히 (여성)"},
        {"value": "ko-KR-InJoonNeural", "label": "인준 (남성)"},
    ],
    "fr": [
        {"value": "fr-FR-DeniseNeural", "label": "Denise (Femme, FR)"},
        {"value": "fr-FR-HenriNeural", "label": "Henri (Homme, FR)"},
        {"value": "fr-CA-SylvieNeural", "label": "Sylvie (Femme, CA)"},
    ],
    "de": [
        {"value": "de-DE-KatjaNeural", "label": "Katja (Weiblich)"},
        {"value": "de-DE-ConradNeural", "label": "Conrad (Männlich)"},
    ],
}

GTTS_LANG_MAP: Dict[str, str] = {
    "vi": "vi",
    "en": "en",
    "ja": "ja",
    "zh": "zh-CN",
    "ko": "ko",
    "fr": "fr",
    "de": "de",
}

SUPPORTED_LANGUAGES = list(EDGE_VOICES.keys())
CJK_LANGUAGES = {"ja", "zh", "ko"}

# ---------------------------------------------------------------------------
# Chunking profiles
# ---------------------------------------------------------------------------
DEFAULT_CHUNK_SIZES = [120, 300, 600, 1500, 3500]
DEFAULT_HARD_MAX_CHUNK = 3500
DEFAULT_MAX_SENTENCES_PER_CHUNK = [2, 4, 8, 15, 50]

CJK_CHUNK_SIZES = [60, 150, 300, 800, 2000]
CJK_HARD_MAX_CHUNK = 2000
CJK_MAX_SENTENCES_PER_CHUNK = [2, 4, 8, 12, 30]

# gTTS không có WordBoundary nên mỗi cue = cả một chunk. Giới hạn chunk
# nhỏ hơn để "tự cuộn theo đoạn" có granularity tốt (client nội suy vị trí
# theo số ký tự bên trong chunk). Cân bằng với số request gửi lên Google.
GTTS_HARD_MAX_CHUNK = 1200

MULTILANG_SENTENCE_RE = re.compile(
    r".+?(?:[.!?…。！？]+(?:[\"'”’»」』）】]*)|$)",
    re.S,
)
PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n+", re.M)
SOFT_SPLIT_RE = re.compile(r"(?<=[,;:…–—，、；：])")
SENTENCE_END_RE = re.compile(r"[.!?…。！？][\"'”’»」』）】]*$")


# ---------------------------------------------------------------------------
# Vietnamese abbreviation protection
# ---------------------------------------------------------------------------
# Placeholder character used to temporarily mask abbreviation periods.
# We use U+E000 (Private Use Area) which never appears in normal text.
_ABBREV_PLACEHOLDER = "\ue000"

# Regex patterns for Vietnamese abbreviations where a period is NOT a sentence
# boundary. Vietnamese abbreviations use mixed case: PGS, TS, Tp, ThS, KTV, ĐT…
# Pattern: word boundary + 2-5 letter abbreviation (mixed case allowed) + period
_ABBREV_WORD = r"[A-ZĐ][A-Za-zĐđÀ-ỹ]{1,5}"
_VI_ABBREV_COMPOUND = re.compile(rf"\b({_ABBREV_WORD})\.({_ABBREV_WORD})\.")
_VI_ABBREV_BEFORE_CAPITAL = re.compile(rf"\b({_ABBREV_WORD})\.\s+(?=[A-ZĐÀ-Ỹ])")
_VI_ABBREV_BEFORE_PUNCT = re.compile(rf"\b({_ABBREV_WORD})\.(?=[,;\n])")
# Abbreviation before a lowercase word — this happens when the abbreviation
# is followed by a common noun, e.g. "KTV. phòng", "ĐT. viễn thông"
# Only match if the word after is short enough to be a common noun (≤3 words).
# We use a whitelist of common abbreviations that precede lowercase words.
_VI_ABBREV_BEFORE_LOWER = re.compile(
    rf"\b(Tp|KTV|ĐT|CN|ĐH|ĐC|UB|UBND|TT|BV|CA|CQ|NV|NS|BCH|HĐ|THPT)\.\s+(?=[a-zà-ỳ])"
)

# Restore pattern: turn placeholder back into period
_ABBREV_RESTORE_RE = re.compile(_ABBREV_PLACEHOLDER)

# ---------------------------------------------------------------------------
# Pydantic
# ---------------------------------------------------------------------------
class TTSRequest(BaseModel):
    text: str
    voice: str = "vi-VN-HoaiMyNeural"
    engine: str = "edge"   # edge | gtts
    language: str = "vi"


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
def md5_short(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]


def get_cache_id(text: str, voice: str, engine: str, language: str) -> str:
    raw = f"{text}_{voice}_{engine}_{language}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def get_audio_path(cache_id: str) -> str:
    return os.path.join(CACHE_DIR, f"{cache_id}.mp3")


def get_meta_path(cache_id: str) -> str:
    return os.path.join(CACHE_DIR, f"{cache_id}.json")


def get_srt_path(cache_id: str) -> str:
    return os.path.join(CACHE_DIR, f"{cache_id}.srt")


def get_vtt_path(cache_id: str) -> str:
    return os.path.join(CACHE_DIR, f"{cache_id}.vtt")


def get_cues_json_path(cache_id: str) -> str:
    return os.path.join(CACHE_DIR, f"{cache_id}.cues.json")


def get_cues_jsonl_path(cache_id: str) -> str:
    return os.path.join(CACHE_DIR, f"{cache_id}.cues.jsonl")


def save_cache_meta(cache_id: str, data: dict) -> None:
    meta_path = get_meta_path(cache_id)
    tmp_path = meta_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp_path, meta_path)


def load_cache_meta(cache_id: str) -> Optional[dict]:
    meta_path = get_meta_path(cache_id)
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def remove_if_exists(path: str) -> bool:
    if os.path.exists(path):
        try:
            os.remove(path)
            return True
        except OSError as exc:
            logger.warning("Không thể xóa file %s: %s", path, exc)
            return False
    return True


def cleanup_incomplete_cache(cache_id: str) -> None:
    """Xóa toàn bộ file cache (audio, meta, subtitle, cues). Best-effort."""
    paths = [
        get_audio_path(cache_id),
        get_meta_path(cache_id),
        get_srt_path(cache_id),
        get_vtt_path(cache_id),
        get_cues_json_path(cache_id),
        get_cues_jsonl_path(cache_id),
    ]
    failed = [p for p in paths if not remove_if_exists(p)]
    if failed:
        logger.warning(
            "cleanup_incomplete_cache(%s): không thể xóa %d file: %s",
            cache_id, len(failed), failed,
        )


def remove_runtime_files_only(cache_id: str) -> None:
    """Xóa file runtime (audio, subtitle, cues) nhưng giữ meta lại. Best-effort."""
    paths = [
        get_audio_path(cache_id),
        get_srt_path(cache_id),
        get_vtt_path(cache_id),
        get_cues_json_path(cache_id),
        get_cues_jsonl_path(cache_id),
    ]
    failed = [p for p in paths if not remove_if_exists(p)]
    if failed:
        logger.warning(
            "remove_runtime_files_only(%s): không thể xóa %d file: %s",
            cache_id, len(failed), failed,
        )


# ---------------------------------------------------------------------------
# Cache pruning — chặn bộ nhớ phình vô hạn
# ---------------------------------------------------------------------------
try:
    MAX_CACHE_MB = int(os.environ.get("MAX_CACHE_MB", "20480"))  # mặc định 20 GB
except ValueError:
    MAX_CACHE_MB = 20480

_CACHE_FILE_RE = re.compile(r"^([a-f0-9]{32})\.(mp3|json|srt|vtt|cues\.json|cues\.jsonl)$")


def prune_cache_dir(
    dir_path: str,
    max_bytes: int,
    target_bytes: Optional[int] = None,
    skip_ids: Optional[set] = None,
) -> int:
    """Xóa các cache cũ nhất (theo mtime) khi tổng dung lượng vượt `max_bytes`.

    - Mỗi cache_id là một nhóm file; mtime của nhóm = mtime cũ nhất trong nhóm.
    - Xóa nhóm cũ nhất trước, tới khi tổng <= `target_bytes` (mặc định 90% max).
    - Bỏ qua các cache_id đang được tạo (`skip_ids`) để không xóa file đang ghi.
    - Trả về số byte đã giải phóng.
    """
    if max_bytes <= 0:
        return 0
    if target_bytes is None:
        target_bytes = int(max_bytes * 0.9)
    skip_ids = skip_ids or set()

    entries = []
    total = 0
    try:
        with os.scandir(dir_path) as it:
            for e in it:
                if not e.is_file(follow_symlinks=False):
                    continue
                m = _CACHE_FILE_RE.match(e.name)
                if not m:
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                entries.append((m.group(1), e.path, st.st_mtime, st.st_size))
                total += st.st_size
    except OSError:
        return 0

    if total <= max_bytes:
        return 0

    groups: Dict[str, list] = {}
    for cid, path, mtime, size in entries:
        groups.setdefault(cid, []).append((path, mtime, size))

    ordered = sorted(
        groups.keys(),
        key=lambda cid: min(m for _, m, _ in groups[cid]),
    )

    freed = 0
    for cid in ordered:
        if total <= target_bytes:
            break
        if cid in skip_ids:
            continue
        removed = 0
        for path, _, size in groups[cid]:
            try:
                os.remove(path)
                removed += size
            except OSError:
                pass
        total -= removed
        freed += removed
    return freed


def is_cache_valid(
    cache_id: str,
    require_subtitles: bool = False,
    require_cues: bool = False,
) -> bool:
    audio_path = get_audio_path(cache_id)
    if not os.path.exists(audio_path):
        return False

    meta = load_cache_meta(cache_id)
    if not meta or meta.get("status") != "completed":
        return False

    # Partial audio (some chunks were skipped) is NOT valid — should be regenerated
    if meta.get("skipped_chunks", 0) > 0:
        return False

    expected_size = meta.get("file_size")
    if expected_size is None:
        return False

    try:
        actual_size = os.path.getsize(audio_path)
    except OSError:
        return False

    if actual_size <= 0 or actual_size != expected_size:
        return False

    if require_subtitles and meta.get("engine") == "edge":
        if not (
            os.path.exists(get_srt_path(cache_id))
            and os.path.exists(get_vtt_path(cache_id))
            and os.path.exists(get_cues_json_path(cache_id))
        ):
            return False

    # Cues are required for reader auto-scroll (edge + gtts chunk cues).
    # Old gtts caches generated without cues are considered invalid → tái tạo.
    if require_cues and not os.path.exists(get_cues_json_path(cache_id)):
        return False

    return True


def get_effective_status(cache_id: str) -> Optional[dict]:
    audio_path = get_audio_path(cache_id)
    file_size = os.path.getsize(audio_path) if os.path.exists(audio_path) else 0

    in_mem = generation_status.get(cache_id)
    if in_mem and in_mem.get("status") in {"queued", "processing", "partial"}:
        status = dict(in_mem)
        status["file_size"] = file_size
        return status

    meta = load_cache_meta(cache_id)
    if meta:
        status = dict(meta)
        status["file_size"] = file_size
        return status

    if is_cache_valid(cache_id):
        # Infer subtitle support from engine stored in meta
        subtitle_supported = meta.get("subtitle_supported", meta.get("engine") == "edge") if meta else False
        subtitle_ready = meta.get("subtitle_ready", subtitle_supported) if meta else False
        scroll_supported = meta.get("scroll_supported", meta.get("engine") in {"edge", "gtts"}) if meta else False
        return {
            "status": "completed",
            "progress": 1,
            "total": 1,
            "file_size": file_size,
            "subtitle_supported": subtitle_supported,
            "subtitle_ready": subtitle_ready,
            "scroll_supported": scroll_supported,
        }

    return None


def strip_id3v2(data: bytes) -> bytes:
    if len(data) >= 10 and data[:3] == b"ID3":
        size = (
            ((data[6] & 0x7F) << 21)
            | ((data[7] & 0x7F) << 14)
            | ((data[8] & 0x7F) << 7)
            | (data[9] & 0x7F)
        )
        return data[size + 10:]
    return data


def normalize_text(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def is_cjk_script_char(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x3040 <= code <= 0x309F
        or 0x30A0 <= code <= 0x30FF
        or 0xFF66 <= code <= 0xFF9D
        or 0xAC00 <= code <= 0xD7AF
        or 0x1100 <= code <= 0x11FF
    )


def char_weight(ch: str) -> int:
    if ch in "\r\n":
        return 0
    if ch.isspace():
        return 1

    east = unicodedata.east_asian_width(ch)
    if east in {"W", "F"} or is_cjk_script_char(ch):
        return 2
    return 1


def effective_len(text: str) -> int:
    return sum(char_weight(ch) for ch in (text or ""))


def is_cjk_language(language: str) -> bool:
    return (language or "").lower().strip() in CJK_LANGUAGES


def is_mostly_cjk(text: str, threshold: float = 0.25) -> bool:
    chars = [ch for ch in (text or "") if not ch.isspace()]
    if not chars:
        return False
    cjk_count = sum(1 for ch in chars if is_cjk_script_char(ch))
    return (cjk_count / len(chars)) >= threshold


def get_chunk_profile(language: str, text: str) -> Tuple[List[int], int, List[int]]:
    if is_cjk_language(language) or is_mostly_cjk(text):
        return CJK_CHUNK_SIZES, CJK_HARD_MAX_CHUNK, CJK_MAX_SENTENCES_PER_CHUNK
    return DEFAULT_CHUNK_SIZES, DEFAULT_HARD_MAX_CHUNK, DEFAULT_MAX_SENTENCES_PER_CHUNK


def smart_join(left: str, right: str) -> str:
    left = (left or "").strip()
    right = (right or "").strip()
    if not left:
        return right
    if not right:
        return left
    if left[-1].isspace() or right[0].isspace():
        return left + right
    if is_cjk_script_char(left[-1]) or is_cjk_script_char(right[0]):
        return left + right
    return f"{left} {right}"


def split_paragraphs(text: str) -> List[str]:
    text = normalize_text(text)
    if not text:
        return []
    return [p.strip() for p in PARAGRAPH_SPLIT_RE.split(text) if p.strip()]


def _protect_abbreviations(text: str) -> str:
    """Replace periods in Vietnamese abbreviations with a placeholder to prevent
    sentence splitting at abbreviation boundaries."""
    P = _ABBREV_PLACEHOLDER
    # Compound: PGS.TS. → PGS\ue000TS\ue000
    text = _VI_ABBREV_COMPOUND.sub(lambda m: m.group(1) + P + m.group(2) + P, text)
    # Before capital: TS. Nguyễn → TS\ue000 Nguyễn
    text = _VI_ABBREV_BEFORE_CAPITAL.sub(lambda m: m.group(1) + P + " ", text)
    # Before lowercase (whitelist): KTV. phòng → KTV\ue000 phòng
    text = _VI_ABBREV_BEFORE_LOWER.sub(lambda m: m.group(1) + P + " ", text)
    # Before punctuation: TS., → TS\ue000,
    text = _VI_ABBREV_BEFORE_PUNCT.sub(lambda m: m.group(1) + P, text)
    return text


def _restore_abbreviations(text: str) -> str:
    """Restore placeholder characters back to periods after sentence splitting."""
    return _ABBREV_RESTORE_RE.sub(".", text)


def split_sentences_multilang(text: str) -> List[str]:
    text = normalize_text(text)
    if not text:
        return []
    # Protect abbreviation periods before splitting
    text = _protect_abbreviations(text)
    matches = [m.group().strip() for m in MULTILANG_SENTENCE_RE.finditer(text)]
    sentences = [_restore_abbreviations(s) for s in matches if s]
    return sentences or [_restore_abbreviations(text)]


def hard_cut_text(text: str, limit: int) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []

    pieces: List[str] = []
    current_chars: List[str] = []
    current_weight = 0

    for ch in text:
        w = char_weight(ch)
        if current_chars and current_weight + w > limit:
            piece = "".join(current_chars).strip()
            if piece:
                pieces.append(piece)
            current_chars = [ch]
            current_weight = w
        else:
            current_chars.append(ch)
            current_weight += w

    if current_chars:
        piece = "".join(current_chars).strip()
        if piece:
            pieces.append(piece)

    return pieces


def pack_units_by_limit(units: List[str], limit: int) -> List[str]:
    chunks: List[str] = []
    current = ""

    for unit in units:
        unit = (unit or "").strip()
        if not unit:
            continue

        if not current:
            if effective_len(unit) <= limit:
                current = unit
            else:
                chunks.extend(hard_cut_text(unit, limit))
            continue

        candidate = smart_join(current, unit)
        if effective_len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current.strip())
            if effective_len(unit) <= limit:
                current = unit
            else:
                sub = hard_cut_text(unit, limit)
                if sub:
                    chunks.extend(sub[:-1])
                    current = sub[-1]
                else:
                    current = ""

    if current.strip():
        chunks.append(current.strip())
    return chunks


def split_long_text_gently_by_space(text: str, limit: int) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    if effective_len(text) <= limit:
        return [text]

    tokens = re.findall(r"\S+\s*", text)
    if not tokens:
        return hard_cut_text(text, limit)

    chunks: List[str] = []
    current = ""

    for token in tokens:
        token = token.strip()
        if not token:
            continue

        if not current:
            if effective_len(token) <= limit:
                current = token
            else:
                chunks.extend(hard_cut_text(token, limit))
            continue

        candidate = smart_join(current, token)
        if effective_len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current.strip())
            if effective_len(token) <= limit:
                current = token
            else:
                sub = hard_cut_text(token, limit)
                if sub:
                    chunks.extend(sub[:-1])
                    current = sub[-1]
                else:
                    current = ""

    if current.strip():
        chunks.append(current.strip())
    return chunks


def split_long_text_gently(text: str, limit: int) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    if effective_len(text) <= limit:
        return [text]

    parts = [p.strip() for p in SOFT_SPLIT_RE.split(text) if p.strip()]
    if len(parts) > 1:
        packed = pack_units_by_limit(parts, limit)
        final_chunks: List[str] = []
        for ch in packed:
            if effective_len(ch) <= limit:
                final_chunks.append(ch)
            else:
                final_chunks.extend(split_long_text_gently_by_space(ch, limit))
        return [c for c in final_chunks if c]

    by_space = split_long_text_gently_by_space(text, limit)
    final_chunks: List[str] = []
    for ch in by_space:
        if effective_len(ch) <= limit:
            final_chunks.append(ch)
        else:
            final_chunks.extend(hard_cut_text(ch, limit))
    return [c for c in final_chunks if c]


def split_text_into_chunks(
    text: str,
    language: str = "vi",
    hard_max_override: Optional[int] = None,
) -> List[str]:
    text = normalize_text(text)
    if not text:
        return []

    chunk_sizes, hard_max, max_sentences_profile = get_chunk_profile(language, text)
    if hard_max_override:
        hard_max = hard_max_override
        chunk_sizes = [min(s, hard_max_override) for s in chunk_sizes]
    paragraphs = split_paragraphs(text)
    if not paragraphs:
        return []

    chunks: List[str] = []
    size_idx = 0
    current = ""
    sentence_count = 0

    for para in paragraphs:
        sentences = split_sentences_multilang(para)
        if not sentences:
            continue

        normalized_units: List[str] = []
        for sentence in sentences:
            if effective_len(sentence) > hard_max:
                normalized_units.extend(split_long_text_gently(sentence, hard_max))
            else:
                normalized_units.append(sentence)

        for unit_idx, unit in enumerate(normalized_units):
            max_len = chunk_sizes[min(size_idx, len(chunk_sizes) - 1)]
            max_sentences = max_sentences_profile[min(size_idx, len(max_sentences_profile) - 1)]

            if not current:
                if effective_len(unit) <= max_len:
                    current = unit
                    sentence_count = 1
                else:
                    sub = split_long_text_gently(unit, max_len)
                    if sub:
                        current = sub[0]
                        sentence_count = 1
                        for rest in sub[1:]:
                            chunks.append(current.strip())
                            size_idx += 1
                            current = rest
                            sentence_count = 1
                continue

            if unit_idx == 0:
                candidate = current + "\n\n" + unit
            else:
                candidate = smart_join(current, unit)

            candidate_sentences = sentence_count + 1
            if effective_len(candidate) <= max_len and candidate_sentences <= max_sentences:
                current = candidate
                sentence_count = candidate_sentences
            else:
                chunks.append(current.strip())
                size_idx += 1
                max_len = chunk_sizes[min(size_idx, len(chunk_sizes) - 1)]
                if effective_len(unit) <= max_len:
                    current = unit
                    sentence_count = 1
                else:
                    sub = split_long_text_gently(unit, max_len)
                    if sub:
                        current = sub[0]
                        sentence_count = 1
                        for rest in sub[1:]:
                            chunks.append(current.strip())
                            size_idx += 1
                            current = rest
                            sentence_count = 1

    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if c.strip()]


def validate_language(language: str) -> str:
    language = (language or "vi").lower().strip()
    if language not in SUPPORTED_LANGUAGES:
        raise HTTPException(status_code=400, detail=f"Unsupported language: {language}")
    return language


def validate_engine(engine: str) -> str:
    engine = (engine or "edge").lower().strip()
    if engine not in {"edge", "gtts"}:
        raise HTTPException(status_code=400, detail="Unsupported engine")
    return engine


def validate_voice(language: str, voice: str, engine: str) -> str:
    if engine == "gtts":
        return ""

    available = EDGE_VOICES.get(language, [])
    if not available:
        raise HTTPException(
            status_code=400,
            detail=f"No voices available for selected language: {language}",
        )

    voice = (voice or "").strip()
    valid_voices = {v["value"] for v in available}
    if voice not in valid_voices:
        raise HTTPException(
            status_code=400,
            detail=f"Voice does not belong to selected language: {language}",
        )
    return voice


# ---------------------------------------------------------------------------
# Subtitle helpers
# ---------------------------------------------------------------------------
def ticks_to_seconds(ticks: int) -> float:
    return max(0.0, float(ticks) / 10_000_000.0)


def smart_join_tokens(parts: List[str]) -> str:
    out = ""
    for part in parts:
        token = (part or "").strip()
        if not token:
            continue
        out = smart_join(out, token) if out else token
    return out.strip()


def group_word_boundaries_to_cues(
    word_boundaries: List[dict],
    base_offset_sec: float,
    language: str,
) -> List[dict]:
    """
    Gom WordBoundary thành cue dễ đọc hơn:
    - Latin: tối đa 8 từ / cue
    - CJK: tối đa 12 token / cue
    - ngắt khi gặp dấu kết câu hoặc quá dài
    """
    if not word_boundaries:
        return []

    is_cjk = is_cjk_language(language)
    max_words = 12 if is_cjk else 8
    max_duration = 3.8 if is_cjk else 4.2

    cues: List[dict] = []
    current_words: List[dict] = []

    def flush_current() -> None:
        nonlocal current_words, cues
        if not current_words:
            return

        start = base_offset_sec + current_words[0]["start"]
        end = base_offset_sec + current_words[-1]["end"]
        text = smart_join_tokens([w["text"] for w in current_words])

        if text:
            end = max(end, start + 0.08)
            cues.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
            })
        current_words = []

    for word in word_boundaries:
        current_words.append(word)

        cue_start = current_words[0]["start"]
        cue_end = current_words[-1]["end"]
        duration = cue_end - cue_start
        token = (word.get("text") or "").strip()
        sentence_end = bool(token and SENTENCE_END_RE.search(token))

        if len(current_words) >= max_words or duration >= max_duration or sentence_end:
            flush_current()

    flush_current()
    return cues


def format_srt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    total_ms = int(round(seconds * 1000))
    hours = total_ms // 3600000
    minutes = (total_ms % 3600000) // 60000
    secs = (total_ms % 60000) // 1000
    millis = total_ms % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_vtt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    total_ms = int(round(seconds * 1000))
    hours = total_ms // 3600000
    minutes = (total_ms % 3600000) // 60000
    secs = (total_ms % 60000) // 1000
    millis = total_ms % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def cues_to_srt(cues: List[dict]) -> str:
    lines: List[str] = []
    for idx, cue in enumerate(cues, start=1):
        lines.append(str(idx))
        lines.append(f"{format_srt_time(cue['start'])} --> {format_srt_time(cue['end'])}")
        lines.append(cue["text"])
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def cues_to_vtt(cues: List[dict]) -> str:
    lines: List[str] = ["WEBVTT", ""]
    for cue in cues:
        lines.append(f"{format_vtt_time(cue['start'])} --> {format_vtt_time(cue['end'])}")
        lines.append(cue["text"])
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def write_json_atomic(path: str, data: object) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def write_text_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def append_cues_jsonl(cache_id: str, cues: List[dict]) -> None:
    if not cues:
        return
    path = get_cues_jsonl_path(cache_id)
    with open(path, "a", encoding="utf-8") as f:
        for cue in cues:
            f.write(json.dumps(cue, ensure_ascii=False) + "\n")


def load_cues_json(cache_id: str) -> List[dict]:
    path = get_cues_json_path(cache_id)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


# ---------------------------------------------------------------------------
# MP3 duration helper
# Dùng để cộng global time offset giữa các chunk cho subtitle.
# ---------------------------------------------------------------------------
BITRATES = {
    (3, 1): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0],
    (3, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0],
    (3, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0],
    (2, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
    (2, 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
    (0, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0],
    (0, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
    (0, 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
}
SAMPLE_RATES = {
    3: [44100, 48000, 32000, 0],
    2: [22050, 24000, 16000, 0],
    0: [11025, 12000, 8000, 0],
}


def skip_id3v2_len(data: bytes) -> int:
    if len(data) >= 10 and data[:3] == b"ID3":
        size = (
            ((data[6] & 0x7F) << 21)
            | ((data[7] & 0x7F) << 14)
            | ((data[8] & 0x7F) << 7)
            | (data[9] & 0x7F)
        )
        return size + 10
    return 0


def mp3_duration_seconds(data: bytes) -> float:
    if not data:
        return 0.0

    pos = skip_id3v2_len(data)
    total = 0.0
    data_len = len(data)

    while pos + 4 <= data_len:
        b1, b2, b3, b4 = data[pos], data[pos + 1], data[pos + 2], data[pos + 3]
        if b1 != 0xFF or (b2 & 0xE0) != 0xE0:
            pos += 1
            continue

        version_bits = (b2 >> 3) & 0x03
        layer_bits = (b2 >> 1) & 0x03
        bitrate_idx = (b3 >> 4) & 0x0F
        sample_rate_idx = (b3 >> 2) & 0x03
        padding = (b3 >> 1) & 0x01

        if version_bits == 1 or layer_bits == 0 or bitrate_idx in {0, 15} or sample_rate_idx == 3:
            pos += 1
            continue

        version_map = {0: 0, 2: 2, 3: 3}
        version = version_map.get(version_bits)
        layer = 4 - layer_bits
        if version is None:
            pos += 1
            continue

        bitrate_table = BITRATES.get((version, layer))
        sample_rates = SAMPLE_RATES.get(version)
        if not bitrate_table or not sample_rates:
            pos += 1
            continue

        bitrate_kbps = bitrate_table[bitrate_idx]
        sample_rate = sample_rates[sample_rate_idx]
        if bitrate_kbps <= 0 or sample_rate <= 0:
            pos += 1
            continue

        if layer == 1:
            samples_per_frame = 384
            frame_length = int((12 * bitrate_kbps * 1000 / sample_rate + padding) * 4)
        elif layer == 2:
            samples_per_frame = 1152
            frame_length = int(144 * bitrate_kbps * 1000 / sample_rate + padding)
        else:
            if version == 3:
                samples_per_frame = 1152
                frame_length = int(144 * bitrate_kbps * 1000 / sample_rate + padding)
            else:
                samples_per_frame = 576
                frame_length = int(72 * bitrate_kbps * 1000 / sample_rate + padding)

        if frame_length <= 0:
            pos += 1
            continue

        total += samples_per_frame / sample_rate
        pos += frame_length

    return round(total, 6)


# ---------------------------------------------------------------------------
# Audio engine helpers
# ---------------------------------------------------------------------------
async def edge_tts_to_audio_and_words(text: str, voice: str, max_retries: int = 3) -> Tuple[bytes, List[dict]]:
    """
    Trả về:
    - audio bytes
    - danh sách WordBoundary / SentenceBoundary đã chuẩn hóa thành start/end/text

    Includes retry logic for NoAudioReceived — Microsoft's Edge TTS WebSocket
    intermittently returns no audio even for valid text. Retry up to max_retries
    times with exponential backoff before giving up on this chunk.
    """
    for attempt in range(max_retries):
        try:
            communicate = edge_tts.Communicate(text, voice, proxy=PROXY)
            audio_parts: List[bytes] = []
            words: List[dict] = []

            async for chunk in communicate.stream():
                chunk_type = chunk.get("type")
                if chunk_type == "audio":
                    audio_parts.append(chunk["data"])
                elif chunk_type in {"WordBoundary", "SentenceBoundary"}:
                    try:
                        start_sec = ticks_to_seconds(int(chunk.get("offset", 0)))
                        duration_sec = ticks_to_seconds(int(chunk.get("duration", 0)))
                        text_part = (chunk.get("text") or "").strip()
                        if text_part:
                            words.append({
                                "type": chunk_type,
                                "text": text_part,
                                "start": start_sec,
                                "end": start_sec + max(0.01, duration_sec),
                            })
                    except Exception:
                        continue

            audio = b"".join(audio_parts)
            if audio:
                return audio, words

            # No audio received but no exception — treat same as NoAudioReceived
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning(
                    "Edge TTS returned empty audio (attempt %d/%d), retrying in %ds for text: %r",
                    attempt + 1, max_retries, wait, text[:80],
                )
                await asyncio.sleep(wait)
            else:
                logger.warning(
                    "Edge TTS returned empty audio after %d retries for text: %r",
                    max_retries, text[:80],
                )
                return b"", []

        except edge_tts.exceptions.NoAudioReceived:
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning(
                    "NoAudioReceived (attempt %d/%d), retrying in %ds for text: %r",
                    attempt + 1, max_retries, wait, text[:80],
                )
                await asyncio.sleep(wait)
            else:
                logger.warning(
                    "NoAudioReceived after %d retries for text: %r",
                    max_retries, text[:80],
                )
                return b"", []

    return b"", []


def gtts_to_bytes(text: str, lang: str = "vi") -> bytes:
    last_exc = None
    for attempt in range(3):
        try:
            tts = gTTS(text=text, lang=lang, timeout=15)
            buf = io.BytesIO()
            tts.write_to_fp(buf)
            return buf.getvalue()
        except Exception as exc:
            last_exc = exc
            error_type = type(exc).__name__
            print(f"[WARN] gTTS attempt {attempt + 1} failed with {error_type}: {exc}")
            if attempt < 2:
                if any(x in error_type.lower() for x in ("socket", "connection", "timeout")):
                    delay = 3 * (attempt + 1)
                else:
                    delay = 2 * (attempt + 1)
                time.sleep(delay)

    raise last_exc


# ---------------------------------------------------------------------------
# Background generation
# ---------------------------------------------------------------------------
async def generate_chunks(
    text: str,
    voice: str,
    engine: str,
    cache_id: str,
    language: str = "vi",
    chunks: Optional[List[str]] = None,
):
    if cache_id not in _generation_locks:
        _generation_locks[cache_id] = asyncio.Lock()

    async with _generation_locks[cache_id]:
        audio_path = get_audio_path(cache_id)
        cues_all: List[dict] = []
        global_audio_sec = 0.0
        cue_index = 0
        skipped_chunks = 0

        require_subtitles = engine == "edge"
        require_cues = engine in {"edge", "gtts"}
        if is_cache_valid(cache_id, require_subtitles=require_subtitles, require_cues=require_cues):
            generation_status[cache_id] = {
                "status": "completed",
                "progress": 1,
                "total": 1,
                "subtitle_supported": engine == "edge",
                "subtitle_ready": engine == "edge",
                "scroll_supported": True,
            }
            return

        if chunks is None:
            if engine == "gtts":
                chunks = split_text_into_chunks(text, language=language, hard_max_override=GTTS_HARD_MAX_CHUNK)
            else:
                chunks = split_text_into_chunks(text, language=language)

        if not chunks:
            err = "Text must not be empty"
            save_cache_meta(
                cache_id,
                {
                    "status": "failed",
                    "error": err,
                    "text_hash": md5_short(text),
                    "voice": voice,
                    "engine": engine,
                    "language": language,
                    "subtitle_supported": engine == "edge",
                    "subtitle_ready": False,
                },
            )
            generation_status.pop(cache_id, None)
            return

        total = len(chunks)

        generation_status[cache_id] = {
            "status": "processing",
            "progress": 0,
            "total": total,
            "subtitle_supported": engine == "edge",
            "subtitle_ready": False,
            "subtitle_cues": 0,
        }
        save_cache_meta(
            cache_id,
            {
                "status": "processing",
                "progress": 0,
                "total": total,
                "text_hash": md5_short(text),
                "voice": voice,
                "engine": engine,
                "language": language,
                "subtitle_supported": engine == "edge",
                "subtitle_ready": False,
                "subtitle_cues": 0,
            },
        )

        # Xóa file runtime cũ nhưng giữ meta vừa save
        remove_runtime_files_only(cache_id)

        loop = asyncio.get_running_loop()

        # Concurrency limit for Edge TTS — 2 parallel connections.
        # More than 2 risks rate-limiting by Microsoft's WebSocket server.
        _EDGE_CONCURRENCY = 2
        edge_semaphore = asyncio.Semaphore(_EDGE_CONCURRENCY) if engine == "edge" else None

        async def _gen_edge_chunk(idx: int, chunk_text: str) -> Tuple[int, bytes, List[dict]]:
            """Generate a single Edge TTS chunk with semaphore-gated concurrency."""
            async with edge_semaphore:  # type: ignore[misc]
                audio, words = await edge_tts_to_audio_and_words(chunk_text, voice)
            return idx, audio, words

        try:
            # ── Edge TTS: pipeline with concurrent prefetch ──
            if engine == "edge":
                # We process chunks in order, but prefetch up to _EDGE_CONCURRENCY
                # chunks ahead so network I/O overlaps with audio assembly.
                pending: Dict[asyncio.Task, int] = {}  # task → chunk index
                next_to_assemble = 0
                # Buffer for out-of-order results: index → (audio, words)
                result_buffer: Dict[int, Tuple[bytes, List[dict]]] = {}

                # Seed initial prefetch tasks
                prefetch_count = min(_EDGE_CONCURRENCY, total)
                for i in range(prefetch_count):
                    task = asyncio.create_task(_gen_edge_chunk(i, chunks[i]))
                    pending[task] = i

                while next_to_assemble < total:
                    # Collect any completed tasks
                    done, _ = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        idx, audio, words = task.result()
                        result_buffer[idx] = (audio, words)
                        del pending[task]

                        # Prefetch next chunk if available
                        next_to_fetch = max(pending.values()) + 1 if pending else next_to_assemble
                        while len(pending) < _EDGE_CONCURRENCY and next_to_fetch < total:
                            t = asyncio.create_task(_gen_edge_chunk(next_to_fetch, chunks[next_to_fetch]))
                            pending[t] = next_to_fetch
                            next_to_fetch += 1

                    # Assemble consecutive results that are ready
                    while next_to_assemble in result_buffer:
                        i = next_to_assemble
                        audio, words = result_buffer.pop(i)
                        next_to_assemble += 1

                        # Prefetch to keep pipeline full
                        while len(pending) < _EDGE_CONCURRENCY and next_to_fetch < total:
                            t = asyncio.create_task(_gen_edge_chunk(next_to_fetch, chunks[next_to_fetch]))
                            pending[t] = next_to_fetch
                            next_to_fetch += 1

                        if not audio:
                            skipped_chunks += 1
                            logger.warning(
                                "Skipping chunk %d/%d (no audio produced): %r",
                                i + 1, total, chunks[i][:80],
                            )
                            continue

                        raw_for_duration = audio
                        if i > 0:
                            audio = strip_id3v2(audio)
                            raw_for_duration = audio

                        with open(audio_path, "ab") as f:
                            f.write(audio)
                            f.flush()

                        chunk_duration = mp3_duration_seconds(raw_for_duration)
                        if chunk_duration <= 0:
                            if words:
                                chunk_duration = max((w["end"] for w in words), default=0.0)
                            if chunk_duration <= 0:
                                chunk_duration = 0.05

                        new_cues = group_word_boundaries_to_cues(words, global_audio_sec, language)
                        for cue in new_cues:
                            cue_index += 1
                            cue["index"] = cue_index

                        cues_all.extend(new_cues)
                        append_cues_jsonl(cache_id, new_cues)
                        generation_status[cache_id]["subtitle_cues"] = len(cues_all)

                        global_audio_sec += chunk_duration

                        current_size = os.path.getsize(audio_path)
                        generation_status[cache_id]["progress"] = i + 1

                        # Save meta every 5 chunks or at the end
                        if (i + 1) % 5 == 0 or i + 1 == total:
                            save_cache_meta(
                                cache_id,
                                {
                                    "status": "processing",
                                    "progress": i + 1,
                                    "total": total,
                                    "current_file_size": current_size,
                                    "text_hash": md5_short(text),
                                    "voice": voice,
                                    "engine": engine,
                                    "language": language,
                                    "subtitle_supported": True,
                                    "subtitle_ready": False,
                                    "subtitle_cues": len(cues_all),
                                },
                            )

            # ── gTTS: sequential (CPU-bound via thread pool) ──
            else:
                for i, chunk_text in enumerate(chunks):
                    gtts_lang = GTTS_LANG_MAP.get(language, "en")
                    audio = await loop.run_in_executor(None, gtts_to_bytes, chunk_text, gtts_lang)

                    if not audio:
                        skipped_chunks += 1
                        logger.warning(
                            "Skipping chunk %d/%d (no audio produced): %r",
                            i + 1, total, chunk_text[:80],
                        )
                        continue

                    raw_for_duration = audio
                    if i > 0:
                        audio = strip_id3v2(audio)

                    with open(audio_path, "ab") as f:
                        f.write(audio)
                        f.flush()

                    # Chunk cue: timing thật từ duration MP3 — dùng cho
                    # "tự cuộn theo đoạn" của khongdich. gTTS không có
                    # WordBoundary nên granularity = cả chunk; client nội suy
                    # vị trí theo số ký tự bên trong chunk.
                    chunk_duration = mp3_duration_seconds(raw_for_duration)
                    if chunk_duration <= 0:
                        chunk_duration = 0.05

                    cue_index += 1
                    cue = {
                        "start": round(global_audio_sec, 3),
                        "end": round(global_audio_sec + chunk_duration, 3),
                        "text": chunk_text,
                        "index": cue_index,
                    }
                    cues_all.append(cue)
                    append_cues_jsonl(cache_id, [cue])
                    global_audio_sec += chunk_duration

                    current_size = os.path.getsize(audio_path)
                    generation_status[cache_id]["progress"] = i + 1
                    generation_status[cache_id]["subtitle_cues"] = len(cues_all)

                    if (i + 1) % 5 == 0 or i + 1 == total:
                        save_cache_meta(
                            cache_id,
                            {
                                "status": "processing",
                                "progress": i + 1,
                                "total": total,
                                "current_file_size": current_size,
                                "text_hash": md5_short(text),
                                "voice": voice,
                                "engine": engine,
                                "language": language,
                                "subtitle_supported": False,
                                "subtitle_ready": False,
                                "scroll_supported": True,
                                "subtitle_cues": len(cues_all),
                            },
                        )

            final_size = os.path.getsize(audio_path) if os.path.exists(audio_path) else 0
            if final_size <= 0:
                raise RuntimeError("Generated audio file is empty")

            subtitle_ready = False
            if cues_all:
                write_json_atomic(get_cues_json_path(cache_id), cues_all)
            if engine == "edge":
                write_text_atomic(get_srt_path(cache_id), cues_to_srt(cues_all))
                write_text_atomic(get_vtt_path(cache_id), cues_to_vtt(cues_all))
                subtitle_ready = True

            final_status = "partial" if skipped_chunks > 0 else "completed"
            save_cache_meta(
                cache_id,
                {
                    "status": final_status,
                    "progress": total,
                    "total": total,
                    "file_size": final_size,
                    "skipped_chunks": skipped_chunks,
                    "text_hash": md5_short(text),
                    "voice": voice,
                    "engine": engine,
                    "language": language,
                    "subtitle_supported": engine == "edge",
                    "subtitle_ready": subtitle_ready,
                    "scroll_supported": engine in {"edge", "gtts"},
                    "subtitle_cues": len(cues_all),
                    "duration_seconds": round(global_audio_sec, 3),
                },
            )
            if final_status == "completed":
                generation_status.pop(cache_id, None)
            else:
                generation_status[cache_id] = {
                    "status": "partial",
                    "progress": total,
                    "total": total,
                    "skipped_chunks": skipped_chunks,
                    "subtitle_supported": engine == "edge",
                    "subtitle_ready": subtitle_ready,
                    "scroll_supported": engine in {"edge", "gtts"},
                    "subtitle_cues": len(cues_all),
                    "duration_seconds": round(global_audio_sec, 3),
                }

        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            print(f"[ERROR] generate_chunks engine={engine}: {err}")

            remove_runtime_files_only(cache_id)
            save_cache_meta(
                cache_id,
                {
                    "status": "failed",
                    "error": err,
                    "text_hash": md5_short(text),
                    "voice": voice,
                    "engine": engine,
                    "language": language,
                    "subtitle_supported": engine == "edge",
                    "subtitle_ready": False,
                    "subtitle_cues": len(cues_all),
                },
            )
            generation_status.pop(cache_id, None)
        finally:
            _generation_locks.pop(cache_id, None)
            # Chống cache phình vô hạn: xóa cache cũ nhất khi vượt MAX_CACHE_MB.
            # Chạy trong executor để không chặn event loop.
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    prune_cache_dir,
                    CACHE_DIR,
                    MAX_CACHE_MB * 1024 * 1024,
                    None,
                    set(generation_status.keys()) | set(_generation_locks.keys()),
                )
            except Exception:  # pragma: no cover
                pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    path = os.path.join(TEMPLATES_DIR, "index.html")
    if not os.path.exists(path):
        return HTMLResponse(
            content="<h3>Template not found.</h3><p>Please create templates/index.html</p>",
            status_code=200,
        )
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__APP_VERSION__", VERSION)
    return HTMLResponse(content=html)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    path_svg = os.path.join(STATIC_DIR, "favicon.svg")
    if os.path.exists(path_svg):
        return FileResponse(path_svg, media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail="favicon not found")


@app.get("/tts/voices")
async def get_voices():
    gtts_labels = {"vi": "Google TTS (Nữ Miền Bắc)"}
    gtts_voices = {}
    for lang in SUPPORTED_LANGUAGES:
        gtts_lang_code = GTTS_LANG_MAP.get(lang, lang)
        label = gtts_labels.get(lang, f"Google TTS ({gtts_lang_code.upper()})")
        gtts_voices[lang] = [
            {
                "value": f"gtts-{gtts_lang_code}",
                "label": label,
                "engine": "gtts",
            }
        ]
    return {
        "voices": EDGE_VOICES,
        "gtts_voices": gtts_voices,
        "languages": SUPPORTED_LANGUAGES,
    }


@app.post("/tts/start")
async def start_tts(background_tasks: BackgroundTasks, request: TTSRequest):
    text = normalize_text(request.text)
    if not text:
        raise HTTPException(status_code=400, detail="Text must not be empty")

    engine = validate_engine(request.engine)
    language = validate_language(request.language)
    voice = validate_voice(language, request.voice, engine)

    cache_id = get_cache_id(text, voice, engine, language)
    require_subtitles = engine == "edge"
    require_cues = engine in {"edge", "gtts"}

    if is_cache_valid(cache_id, require_subtitles=require_subtitles, require_cues=require_cues):
        return {
            "cache_id": cache_id,
            "status": "completed",
            "url": f"/tts/file/{cache_id}",
            "subtitle_supported": engine == "edge",
            "subtitle_ready": engine == "edge",
            "scroll_supported": True,
        }

    current = get_effective_status(cache_id)
    if current and current.get("status") in {"queued", "processing"}:
        return {
            "cache_id": cache_id,
            "status": current.get("status"),
            "subtitle_supported": engine == "edge",
            "subtitle_ready": False,
            "scroll_supported": require_cues,
        }

    if os.path.exists(get_audio_path(cache_id)) or os.path.exists(get_meta_path(cache_id)):
        cleanup_incomplete_cache(cache_id)

    if engine == "gtts":
        chunk_preview = split_text_into_chunks(text, language=language, hard_max_override=GTTS_HARD_MAX_CHUNK)
    else:
        chunk_preview = split_text_into_chunks(text, language=language)

    generation_status[cache_id] = {
        "status": "queued",
        "progress": 0,
        "total": len(chunk_preview),
        "subtitle_supported": engine == "edge",
        "subtitle_ready": False,
        "subtitle_cues": 0,
    }
    save_cache_meta(
        cache_id,
        {
            "status": "queued",
            "progress": 0,
            "total": len(chunk_preview),
            "text_hash": md5_short(text),
            "voice": voice,
            "engine": engine,
            "language": language,
            "subtitle_supported": engine == "edge",
            "subtitle_ready": False,
            "subtitle_cues": 0,
        },
    )

    background_tasks.add_task(generate_chunks, text, voice, engine, cache_id, language, chunk_preview)

    return {
        "cache_id": cache_id,
        "status": "started",
        "estimated_chunks": len(chunk_preview),
        "subtitle_supported": engine == "edge",
        "subtitle_ready": False,
        "scroll_supported": engine in {"edge", "gtts"},
    }




@app.post("/tts/regenerate")
async def regenerate_tts(background_tasks: BackgroundTasks, request: TTSRequest):
    """Force re-generate audio: delete existing cache, then start fresh generation."""
    text = normalize_text(request.text)
    if not text:
        raise HTTPException(status_code=400, detail="Text must not be empty")

    engine = validate_engine(request.engine)
    language = validate_language(request.language)
    voice = validate_voice(language, request.voice, engine)

    cache_id = get_cache_id(text, voice, engine, language)

    # Clean up any existing cache (completed, partial, failed, or incomplete)
    cleanup_incomplete_cache(cache_id)
    # Also remove from in-memory status
    generation_status.pop(cache_id, None)

    if engine == "gtts":
        chunk_preview = split_text_into_chunks(text, language=language, hard_max_override=GTTS_HARD_MAX_CHUNK)
    else:
        chunk_preview = split_text_into_chunks(text, language=language)

    generation_status[cache_id] = {
        "status": "queued",
        "progress": 0,
        "total": len(chunk_preview),
        "subtitle_supported": engine == "edge",
        "subtitle_ready": False,
        "subtitle_cues": 0,
    }
    save_cache_meta(
        cache_id,
        {
            "status": "queued",
            "progress": 0,
            "total": len(chunk_preview),
            "text_hash": md5_short(text),
            "voice": voice,
            "engine": engine,
            "language": language,
            "subtitle_supported": engine == "edge",
            "subtitle_ready": False,
            "subtitle_cues": 0,
        },
    )

    background_tasks.add_task(generate_chunks, text, voice, engine, cache_id, language, chunk_preview)

    return {
        "cache_id": cache_id,
        "status": "started",
        "estimated_chunks": len(chunk_preview),
        "regenerated": True,
        "subtitle_supported": engine == "edge",
        "subtitle_ready": False,
        "scroll_supported": engine in {"edge", "gtts"},
    }


@app.get("/tts/status/{cache_id}")
async def get_status(cache_id: str):
    cache_id = validate_cache_id(cache_id)
    status = get_effective_status(cache_id)
    if not status:
        raise HTTPException(status_code=404, detail="Not found")
    return status


@app.get("/tts/file/{cache_id}")
async def get_audio_file(cache_id: str, request: Request):
    cache_id = validate_cache_id(cache_id)
    if not is_cache_valid(cache_id):
        status = get_effective_status(cache_id)
        if status and status.get("status") in {"queued", "processing"}:
            raise HTTPException(
                status_code=409,
                detail="Audio is still being generated. Use /tts/stream for live playback.",
            )
        raise HTTPException(status_code=404, detail="Audio not ready")

    audio_path = get_audio_path(cache_id)
    return FileResponse(
        audio_path,
        media_type="audio/mpeg",
        filename=f"{cache_id}.mp3",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


@app.get("/tts/subtitle/srt/{cache_id}")
async def get_srt_file(cache_id: str):
    cache_id = validate_cache_id(cache_id)
    meta = load_cache_meta(cache_id)
    if not meta or meta.get("engine") != "edge":
        raise HTTPException(status_code=404, detail="Subtitle not found")

    if not is_cache_valid(cache_id, require_subtitles=True):
        status = get_effective_status(cache_id)
        if status and status.get("status") in {"queued", "processing"}:
            raise HTTPException(status_code=409, detail="Subtitle is still being generated")
        raise HTTPException(status_code=404, detail="Subtitle not ready")

    return FileResponse(
        get_srt_path(cache_id),
        media_type="application/x-subrip",
        filename=f"{cache_id}.srt",
    )


@app.get("/tts/subtitle/vtt/{cache_id}")
async def get_vtt_file(cache_id: str):
    cache_id = validate_cache_id(cache_id)
    meta = load_cache_meta(cache_id)
    if not meta or meta.get("engine") != "edge":
        raise HTTPException(status_code=404, detail="Subtitle not found")

    if not is_cache_valid(cache_id, require_subtitles=True):
        status = get_effective_status(cache_id)
        if status and status.get("status") in {"queued", "processing"}:
            raise HTTPException(status_code=409, detail="Subtitle is still being generated")
        raise HTTPException(status_code=404, detail="Subtitle not ready")

    return FileResponse(
        get_vtt_path(cache_id),
        media_type="text/vtt",
        filename=f"{cache_id}.vtt",
    )


@app.get("/tts/cues/{cache_id}")
async def get_cues(cache_id: str):
    cache_id = validate_cache_id(cache_id)
    meta = load_cache_meta(cache_id)
    engine = meta.get("engine") if meta else None
    if engine not in {"edge", "gtts"}:
        return JSONResponse(
            {"cues": [], "done": True, "subtitle_supported": False, "scroll_supported": False}
        )
    subtitle_supported = engine == "edge"

    cues = load_cues_json(cache_id)
    if cues:
        return {
            "cues": cues,
            "done": True,
            "subtitle_supported": subtitle_supported,
            "scroll_supported": True,
        }

    jsonl_path = get_cues_jsonl_path(cache_id)
    partial: List[dict] = []

    if os.path.exists(jsonl_path):
        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        partial.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass

    status = get_effective_status(cache_id)
    if status and status.get("status") in {"completed", "failed"}:
        result: dict = {
            "cues": partial,
            "done": True,
            "subtitle_supported": subtitle_supported,
            "scroll_supported": True,
        }
        if status.get("status") == "failed":
            result["error"] = status.get("error", "generation failed")
        return result
    return {
        "cues": partial,
        "done": False,
        "subtitle_supported": subtitle_supported,
        "scroll_supported": True,
    }


@app.get("/tts/cues/stream/{cache_id}")
async def stream_cues_live(cache_id: str, request: Request):
    cache_id = validate_cache_id(cache_id)
    meta = load_cache_meta(cache_id)
    engine = meta.get("engine") if meta else None
    subtitle_supported = engine == "edge"
    scroll_supported = engine in {"edge", "gtts"}
    if engine not in {"edge", "gtts"}:
        async def empty():
            payload = json.dumps(
                {"done": True, "subtitle_supported": False, "scroll_supported": False},
                ensure_ascii=False,
            )
            yield f"event: complete\ndata: {payload}\n\n"

        return StreamingResponse(
            empty(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
                "X-Accel-Buffering": "no",
            },
        )

    # Hỗ trợ resume: đọc Last-Event-ID từ header để skip cues đã nhận
    last_event_id = request.headers.get("Last-Event-ID")
    resume_after_index = 0
    if last_event_id:
        try:
            resume_after_index = int(last_event_id)
        except (ValueError, TypeError):
            pass

    async def event_generator() -> AsyncGenerator[str, None]:
        jsonl_path = get_cues_jsonl_path(cache_id)
        sent_offset = 0
        last_heartbeat = time.time()
        stable_completed_checks = 0

        def _emit_cue_line(payload: dict) -> Optional[str]:
            """Tạo SSE event string cho cue, hoặc None nếu cue đã được gửi."""
            cue_idx = payload.get("index", 0)
            if cue_idx <= resume_after_index:
                return None
            event_id = str(cue_idx)
            data_str = json.dumps(payload, ensure_ascii=False)
            return f"id: {event_id}\nevent: cue\ndata: {data_str}\n\n"

        while True:
            if os.path.exists(jsonl_path):
                try:
                    with open(jsonl_path, "r", encoding="utf-8") as f:
                        f.seek(sent_offset)
                        while True:
                            line = f.readline()
                            if not line:
                                break
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                payload = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            evt = _emit_cue_line(payload)
                            if evt:
                                yield evt
                            sent_offset = f.tell()
                except OSError:
                    pass

            st = get_effective_status(cache_id)
            state = st.get("status") if st else None

            if state == "failed":
                payload = json.dumps({"error": st.get("error", "generation failed")}, ensure_ascii=False)
                yield f"event: error\ndata: {payload}\n\n"
                break

            if state == "completed":
                # kiểm tra lần nữa xem còn cue cuối nào chưa flush hết không
                if os.path.exists(jsonl_path):
                    try:
                        with open(jsonl_path, "r", encoding="utf-8") as f:
                            f.seek(sent_offset)
                            has_more = False
                            while True:
                                line = f.readline()
                                if not line:
                                    break
                                has_more = True
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    payload = json.loads(line)
                                except json.JSONDecodeError:
                                    continue
                                evt = _emit_cue_line(payload)
                                if evt:
                                    yield evt
                                sent_offset = f.tell()

                            if not has_more:
                                stable_completed_checks += 1
                    except OSError:
                        stable_completed_checks += 1
                else:
                    stable_completed_checks += 1

                if stable_completed_checks >= 2:
                    payload = json.dumps(
                        {
                            "done": True,
                            "subtitle_supported": subtitle_supported,
                            "scroll_supported": scroll_supported,
                        },
                        ensure_ascii=False,
                    )
                    yield f"event: complete\ndata: {payload}\n\n"
                    break
            else:
                stable_completed_checks = 0

            if time.time() - last_heartbeat >= 10:
                yield ": keep-alive\n\n"
                last_heartbeat = time.time()

            await asyncio.sleep(0.25)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/tts/stream/{cache_id}")
async def stream_audio_live(cache_id: str):
    cache_id = validate_cache_id(cache_id)
    audio_path = get_audio_path(cache_id)

    # Chờ đến khi có byte đầu tiên
    for _ in range(150):  # ~15 giây
        if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
            break

        st = get_effective_status(cache_id)
        if st and st.get("status") == "failed":
            err = st.get("error", "generation failed")
            raise HTTPException(status_code=503, detail=f"Generation failed: {err}")

        await asyncio.sleep(0.1)
    else:
        st = get_effective_status(cache_id)
        if st and st.get("status") == "failed":
            err = st.get("error", "generation failed")
            raise HTTPException(status_code=503, detail=f"Generation failed: {err}")
        if st and st.get("status") in {"queued", "processing", "generating"}:
            raise HTTPException(
                status_code=503,
                detail="Audio is still being generated, retry later.",
                headers={"Retry-After": "5"},
            )
        raise HTTPException(status_code=404, detail="Audio not ready")

    async def generate() -> AsyncGenerator[bytes, None]:
        read_size = 64 * 1024
        sent = 0
        stable_completed_checks = 0

        while True:
            if os.path.exists(audio_path):
                cur_size = os.path.getsize(audio_path)
                if cur_size > sent:
                    with open(audio_path, "rb") as f:
                        f.seek(sent)
                        while sent < cur_size:
                            chunk = f.read(min(read_size, cur_size - sent))
                            if not chunk:
                                break
                            sent += len(chunk)
                            yield chunk
                    stable_completed_checks = 0

            st = get_effective_status(cache_id)
            state = st.get("status") if st else None

            if state == "failed":
                break

            if state == "completed":
                expected_size = st.get("file_size") or 0
                if expected_size > 0 and sent >= expected_size:
                    stable_completed_checks += 1
                    if stable_completed_checks >= 2:
                        break
                else:
                    stable_completed_checks = 0

            await asyncio.sleep(0.2)

    return StreamingResponse(
        generate(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Optional debug route
# ---------------------------------------------------------------------------
@app.post("/tts/debug/chunks")
async def debug_chunks(request: TTSRequest):
    if not ENABLE_DEBUG_TTS:
        raise HTTPException(status_code=404, detail="Not found")

    text = normalize_text(request.text)
    if not text:
        raise HTTPException(status_code=400, detail="Text must not be empty")

    language = validate_language(request.language)
    chunk_sizes, hard_max, max_sentences_profile = get_chunk_profile(language, text)
    chunks = split_text_into_chunks(text, language=language)

    return {
        "language": language,
        "chunk_profile": {
            "chunk_sizes": chunk_sizes,
            "hard_max": hard_max,
            "max_sentences_per_chunk": max_sentences_profile,
        },
        "total_chunks": len(chunks),
        "chunks": [
            {
                "index": i + 1,
                "effective_len": effective_len(ch),
                "char_len": len(ch),
                "text": ch,
            }
            for i, ch in enumerate(chunks)
        ],
    }


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"ok": True, "version": VERSION}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)