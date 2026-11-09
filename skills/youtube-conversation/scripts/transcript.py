#!/usr/bin/env python3
"""
transcript.py - helper for the youtube-conversation skill.

  fetch  VIDEO [--lang en,any] [--out FILE]
         Download the real transcript of a YouTube video (URL or 11-char ID)
         with the youtube-transcript-api package and save it as JSON.
  parse  FILE|- [--video URL] [--title T] [--out FILE]
         Normalise a pasted YouTube transcript or an .srt/.vtt/.txt file and
         save it as JSON (same format as fetch).
  show   SRC [--stats] [--from T] [--to T] [--max-chars N]
         Print the transcript as "[mm:ss] text" lines (or just stats).
  search SRC TERM [TERM ...] [--context N] [--max N]
         Find where any of the terms is said; prints timestamp links.
  link   SRC T [T ...]
         Turn timestamps (754, 12:34, 1:02:03) into markdown timestamp links.
  cleanup [--dry-run]
         Remove expired or surplus saved transcripts from the default store.

Global option: -v / --verbose prints structured debug logs (never transcript text) to stderr.

SRC is a saved transcript JSON, or the YouTube URL / video ID it was saved for.
T accepts seconds (754), M:SS (12:34) or H:MM:SS (1:02:03).

Storage: transcripts saved without --out go to <system temp>/youtube-conversation/ (or the folder named
by TUBEGPT_STORAGE_DIR). Files there are removed automatically when older than TUBEGPT_RETENTION_HOURS
(default 24, counted from last use) and the oldest are removed when the folder exceeds
TUBEGPT_MAX_STORE_MB (default 200) or TUBEGPT_MAX_FILES (default 500). Files written with --out are never
touched by cleanup.

Exit codes: 0 ok | 1 unexpected error | 2 bad input | 3 cannot reach YouTube |
            4 no readable transcript for this video | 5 YouTube refused the
            request (blocked / rate limited) | 6 missing or outdated dependency |
            7 transcript storage unavailable, not writable or full
"""
from __future__ import annotations

import argparse
import bisect
import errno
import hashlib
import html
import json
import logging
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

EXIT_ERROR, EXIT_BAD_INPUT, EXIT_NETWORK = 1, 2, 3
EXIT_NO_TRANSCRIPT, EXIT_BLOCKED, EXIT_MISSING_DEP = 4, 5, 6
EXIT_STORAGE = 7

log = logging.getLogger("tubegpt")

ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
TS = r"(?:\d{1,2}:)?\d{1,2}:\d{2}"
TS_CUE = r"(?:\d{1,4}:)?\d{1,2}:\d{2}(?:[.,]\d{1,9})?"
CUE_RE = re.compile(r"^\s*(" + TS_CUE + r")\s*-->\s*(" + TS_CUE + r")")
PANEL_RE = re.compile(r"^\s*(" + TS + r")(?!\d)\s*[-\u2013\u2014\u00b7\u2022|:]?\s*(.*)$")
CUE_ID_RE = re.compile(r"^\s*\d{1,9}\s*$")
# Only real caption markup is stripped; a spoken "a < b and c > d" must survive.
CUE_TAG_RE = re.compile(
    r"</?(?:i|b|u|c|v|ruby|rt|lang|font|span|br|p)(?:[.\s][^<>]*)?/?>"
    r"|<\d{1,4}:\d{2}(?::\d{2})?[.,]\d{1,3}>"
)

# Resource limits (inputs, saved documents and numbers).
MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_DOC_BYTES = 100 * 1024 * 1024
MAX_SEGMENTS = 500_000
MAX_SEGMENT_CHARS = 100_000
MAX_SECONDS = 10_000_000  # about 115 days: far beyond any YouTube video, still rules out absurd values
MAX_TERMS, MAX_TERM_CHARS, MAX_LINK_TIMES = 50, 500, 200
MAX_LANG_CODES = 20

# Rolling-caption handling (see parse_cues).
ROLLING_GAP_S = 0.25
FILLER_MAX_S = 0.05

# Storage defaults, all overridable through environment variables (see module docstring).
STORE_MARKER = ".tubegpt-store"
STORE_FILE_RE = re.compile(r"^(?:[A-Za-z0-9_-]{11}|pasted-[0-9a-f]{8})\.json$")
TMP_FILE_RE = re.compile(r"^\.tmp-[a-z0-9_]{8}\.json$")
DEFAULT_RETENTION_HOURS = 24.0
DEFAULT_MAX_STORE_MB = 200
DEFAULT_MAX_FILES = 500
CLEANUP_GRACE_S = 300  # files touched more recently than this are treated as in use and never deleted


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def die(message: str, code: int = EXIT_ERROR):
    print("ERROR: " + message, file=sys.stderr)
    raise SystemExit(code)


def extract_video_id(value: str):
    """Return the 11-character video ID from a YouTube URL or bare ID, else None."""
    value = (value or "").strip()
    if ID_RE.match(value):
        return value
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        value = "https://" + value
    try:
        url = urlparse(value)
        host = (url.hostname or "").lower()
    except ValueError:
        return None
    candidate = ""
    is_youtube = any(host == d or host.endswith("." + d) for d in ("youtube.com", "youtube-nocookie.com"))
    if host in ("youtu.be", "www.youtu.be"):
        candidate = url.path.lstrip("/").split("/")[0]
    elif is_youtube:
        parts = [p for p in url.path.split("/") if p]
        if url.path.rstrip("/") == "/watch":
            candidate = parse_qs(url.query).get("v", [""])[0]
        elif len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
            candidate = parts[1]
    return candidate if ID_RE.match(candidate) else None


def to_seconds(ts: str) -> float:
    nums = [float(p) for p in ts.strip().replace(",", ".").split(":")]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0]


# Accepted user-typed times: seconds (754, 754.5), M:SS (12:34) and H:MM:SS (1:02:03). In the M:SS and
# H:MM:SS forms the seconds (and, with hours, the minutes) must be 0-59; a comma may replace the decimal point.
TIME_ARG_RE = re.compile(
    r"^(?:(?P<sec_only>[0-9]+(?:\.[0-9]+)?)"
    r"|(?P<m>[0-9]+):(?P<s>[0-5]?[0-9](?:\.[0-9]+)?)"
    r"|(?P<h>[0-9]+):(?P<mm>[0-5]?[0-9]):(?P<ss>[0-5]?[0-9](?:\.[0-9]+)?))$"
)


def parse_time_arg(value: str) -> float:
    text = value.strip().replace(",", ".") if isinstance(value, str) else ""
    if re.search(r"(^|:)\s*-", text):
        die(f"Time cannot be negative: {value!r}", EXIT_BAD_INPUT)
    if not TIME_ARG_RE.match(text):
        die(f"Not a valid time: {value!r}. Use seconds (754), M:SS (12:34) or H:MM:SS (1:02:03); "
            "minutes and seconds must be below 60 when hours or minutes are given.", EXIT_BAD_INPUT)
    seconds = to_seconds(text)
    if seconds > MAX_SECONDS:
        die(f"Time is too large: {value!r} (the maximum is {MAX_SECONDS} seconds).", EXIT_BAD_INPUT)
    return seconds


def fmt_time(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def link_url(video_id, seconds: float) -> str:
    return f"https://youtu.be/{video_id}?t={int(seconds)}"


def clean_text(text: str, cue: bool = False) -> str:
    if cue:  # caption files carry <i>, <c>, <00:00:01.500> tags and HTML entities
        text = CUE_TAG_RE.sub("", text)
        text = html.unescape(text)
    # Zero-width space and BOM are noise; zero-width joiners are kept because Indic scripts need them.
    text = text.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def split_lines(text: str):
    """Split on real line breaks only (str.splitlines also splits on U+2028, form feeds and others)."""
    return re.split(r"\r\n|\r|\n", text)


def make_doc(segments, *, source, video_id=None, language=None, is_generated=None,
             title=None, channel=None):
    stamps = [s["start"] for s in segments if s.get("start") is not None]
    ends = [s.get("end") if s.get("end") is not None else s["start"]
            for s in segments if s.get("start") is not None]
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
        "title": title,
        "channel": channel,
        "language": language,
        "is_generated": is_generated,
        "source": source,
        "has_timestamps": bool(stamps),
        "duration_s": max(ends) if ends else None,
        "segments": segments,
    }


# --------------------------------------------------------------------------
# storage: where saved transcripts live, how they are written and when they are removed
# --------------------------------------------------------------------------
def default_dir() -> Path:
    custom = os.environ.get("TUBEGPT_STORAGE_DIR", "").strip()
    if custom:
        return Path(custom).expanduser()
    return Path(tempfile.gettempdir()) / "youtube-conversation"


def custom_storage() -> bool:
    """True when the user pointed the store at their own folder (which may hold unrelated files)."""
    return bool(os.environ.get("TUBEGPT_STORAGE_DIR", "").strip())


def _env_number(name: str, default, lo, hi, integer: bool):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        die(f"{name} must be a number between {lo} and {hi}, not {raw!r}.", EXIT_BAD_INPUT)
    if not math.isfinite(value) or not lo <= value <= hi:
        die(f"{name} must be between {lo} and {hi} (got {raw!r}).", EXIT_BAD_INPUT)
    return value


def storage_limits():
    """Return (retention seconds, maximum total bytes, maximum file count) from the environment."""
    hours = _env_number("TUBEGPT_RETENTION_HOURS", DEFAULT_RETENTION_HOURS, 0.1, 8760, False)
    megabytes = _env_number("TUBEGPT_MAX_STORE_MB", DEFAULT_MAX_STORE_MB, 1, 10240, True)
    files = _env_number("TUBEGPT_MAX_FILES", DEFAULT_MAX_FILES, 1, 10000, True)
    return hours * 3600, megabytes * 1024 * 1024, files


def storage_error(exc: OSError, action: str):
    """Turn an OSError from the storage layer into a clear, path-free message (exit code 7)."""
    if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
        reason = "There is not enough free disk space"
    elif exc.errno == errno.EROFS:
        reason = "The storage location is read-only"
    elif isinstance(exc, PermissionError):
        reason = "Permission was denied"
    else:
        reason = f"The operation failed ({type(exc).__name__})"
    die(f"{reason} while trying to {action}. Free some space, set TUBEGPT_STORAGE_DIR to another folder, "
        "or choose another location with --out.", EXIT_STORAGE)


def ensure_store(create: bool = True) -> Path:
    """Return the transcript store folder, creating it privately and refusing unsafe ones.

    The shared default location is refused if it is a symbolic link or belongs to another user, so a local
    attacker cannot pre-create it to read or redirect transcripts. A folder chosen with TUBEGPT_STORAGE_DIR
    may hold unrelated files, so cleanup only runs there once it carries our marker file, which is written
    only when we created the folder or found it empty.
    """
    store = default_dir()
    custom = custom_storage()
    try:
        if not custom and store.is_symlink():
            die("The transcript storage folder is a symbolic link, so it was not used.", EXIT_STORAGE)
        created = False
        if not store.exists():
            if not create:
                return store
            store.mkdir(parents=True, mode=0o700)
            created = True
        elif not store.is_dir():
            die("The transcript storage location exists but is not a folder.", EXIT_STORAGE)
        info = store.stat()
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            die("The transcript storage folder belongs to another user, so it was not used.", EXIT_STORAGE)
        if os.name == "posix" and info.st_mode & 0o022:
            store.chmod(0o700)
        marker = store / STORE_MARKER
        if custom and not marker.exists() and (created or not any(store.iterdir())):
            marker.write_text("tubeGPT transcript store: files here may be removed by cleanup.\n", encoding="utf-8")
    except OSError as exc:
        storage_error(exc, "prepare the transcript storage folder")
    return store


def store_file(name: str) -> Path:
    """Path of a store file. Only names this program generates are accepted, so '..' or slashes cannot pass."""
    if not STORE_FILE_RE.match(name):
        die("Not a valid saved-transcript name.", EXIT_BAD_INPUT)
    store = default_dir()
    path = store / name
    if path.parent != store:
        die("Not a valid saved-transcript name.", EXIT_BAD_INPUT)
    return path


def write_json_atomic(path, doc):
    """Write JSON through a private temporary file in the same folder, then rename it into place.

    A crash or a full disk never leaves a half-written transcript, and the temporary file is always removed.
    """
    data = json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")
    if len(data) > MAX_DOC_BYTES:
        die(f"The transcript is too large to save ({len(data) // (1024 * 1024)} MB; the limit is "
            f"{MAX_DOC_BYTES // (1024 * 1024)} MB).", EXIT_BAD_INPUT)
    path = Path(path)
    tmp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=path.parent)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
        tmp_name = None
    except OSError as exc:
        storage_error(exc, "save the transcript")
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError as exc:
                log.debug("event=temp_cleanup_failed error=%s", type(exc).__name__)


def cleanup_store(store, *, keep=(), dry_run: bool = False, now=None):
    """Delete expired and surplus transcripts from the store. Returns a summary dict.

    Safety rules, all covered by tests:
    - for a folder chosen with TUBEGPT_STORAGE_DIR, only acts when it carries our marker file;
    - only looks at regular files directly inside the store (no recursion, symbolic links ignored);
    - only deletes names this program generates (<video id>.json, pasted-<hash>.json, leftover .tmp-*.json);
    - never deletes a file in ``keep`` or one used (loaded or saved) within the last CLEANUP_GRACE_S seconds;
    - uses os.unlink on single files only, never recursive deletion.
    """
    store = Path(store)
    result = {"expired": 0, "over_limit": 0, "freed_bytes": 0, "remaining": 0, "dry_run": dry_run}
    if custom_storage() and not (store / STORE_MARKER).is_file():
        log.debug("event=cleanup_skipped reason=no_marker")
        return result
    retention_s, max_bytes, max_files = storage_limits()
    now = time.time() if now is None else now
    keep_names = {Path(k).name for k in keep}
    entries = []  # (mtime, name, size, is_tmp)
    try:
        listing = list(os.scandir(store))
    except OSError as exc:
        log.warning("Could not list the transcript store for cleanup (%s).", type(exc).__name__)
        return result
    for entry in listing:
        is_tmp = bool(TMP_FILE_RE.match(entry.name))
        if not (is_tmp or STORE_FILE_RE.match(entry.name)):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        entries.append((info.st_mtime, entry.name, info.st_size, is_tmp))
    entries.sort()

    def protected(mtime, name):
        return name in keep_names or now - mtime < CLEANUP_GRACE_S

    removed = set()

    def remove(mtime, name, size, kind):
        if dry_run:
            removed.add(name)
        else:
            try:
                os.unlink(store / name)
            except OSError as exc:
                log.warning("Could not remove an old transcript file (%s).", type(exc).__name__)
                return False
            removed.add(name)
        result[kind] += 1
        result["freed_bytes"] += size
        return True

    for mtime, name, size, is_tmp in entries:
        limit = CLEANUP_GRACE_S if is_tmp else retention_s
        if not protected(mtime, name) and now - mtime > limit:
            remove(mtime, name, size, "expired")
    live = [e for e in entries if e[1] not in removed and not e[3]]
    total, count = sum(e[2] for e in live), len(live)
    for mtime, name, size, _ in live:
        if total <= max_bytes and count <= max_files:
            break
        if not protected(mtime, name) and remove(mtime, name, size, "over_limit"):
            total -= size
            count -= 1
    result["remaining"] = count
    log.debug("event=cleanup expired=%d over_limit=%d freed_bytes=%d remaining=%d dry_run=%s",
              result["expired"], result["over_limit"], result["freed_bytes"], count, dry_run)
    return result


def save_doc(doc, out):
    if out:
        path = Path(out)
        if path.is_dir():
            die("--out must be a file path, but it points to a folder.", EXIT_BAD_INPUT)
        write_json_atomic(path, doc)
        return path
    name = doc.get("video_id")
    if not name:
        text = "".join(s["text"] for s in doc["segments"]).encode("utf-8")
        name = "pasted-" + hashlib.sha1(text, usedforsecurity=False).hexdigest()[:8]
    ensure_store()
    path = store_file(f"{name}.json")
    cleanup_store(default_dir(), keep=[path])
    write_json_atomic(path, doc)
    return path


# --------------------------------------------------------------------------
# loading and validating saved transcripts
# --------------------------------------------------------------------------
class InvalidDocument(ValueError):
    """A saved transcript that does not have the structure this tool writes."""


def _number(value, what, *, lo=0.0, hi=float(MAX_SECONDS)):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidDocument(f"{what} must be a number")
    if not math.isfinite(value):
        raise InvalidDocument(f"{what} must be a finite number")
    if not lo <= value <= hi:
        raise InvalidDocument(f"{what} is out of range")
    return value


def _optional_text(doc, key, max_len):
    value = doc.get(key)
    if value is not None and (not isinstance(value, str) or len(value) > max_len):
        raise InvalidDocument(f"'{key}' must be text of at most {max_len} characters or null")


def validate_doc(doc):
    """Check a loaded transcript document and return it (with has_timestamps filled in if missing).

    Accepts everything fetch and parse have ever written. Unknown extra keys are ignored so newer files
    stay readable; wrong types, bad numbers and impossible timing are rejected with InvalidDocument.
    """
    if not isinstance(doc, dict):
        raise InvalidDocument("the file does not contain a JSON object")
    segments = doc.get("segments")
    if not isinstance(segments, list):
        raise InvalidDocument("'segments' is missing or is not a list")
    if not segments:
        raise InvalidDocument("it contains no transcript segments")
    if len(segments) > MAX_SEGMENTS:
        raise InvalidDocument(f"it has more than {MAX_SEGMENTS} segments")
    video_id = doc.get("video_id")
    if video_id is not None and not (isinstance(video_id, str) and ID_RE.match(video_id)):
        raise InvalidDocument("'video_id' is not a valid YouTube video ID")
    for key, limit in (("url", 2048), ("title", 1000), ("channel", 1000), ("language", 35), ("source", 200)):
        _optional_text(doc, key, limit)
    if doc.get("is_generated") is not None and not isinstance(doc["is_generated"], bool):
        raise InvalidDocument("'is_generated' must be true, false or null")
    if doc.get("duration_s") is not None:
        _number(doc["duration_s"], "'duration_s'")
    flag = doc.get("has_timestamps")
    if flag is not None and not isinstance(flag, bool):
        raise InvalidDocument("'has_timestamps' must be true or false")

    timed = 0
    previous_start = None
    for index, seg in enumerate(segments):
        where = f"segment {index}"
        if not isinstance(seg, dict):
            raise InvalidDocument(f"{where} is not an object")
        text = seg.get("text")
        if not isinstance(text, str):
            raise InvalidDocument(f"{where} has no text, or its text is not a string")
        if len(text) > MAX_SEGMENT_CHARS:
            raise InvalidDocument(f"{where} text is longer than {MAX_SEGMENT_CHARS} characters")
        start = seg.get("start")
        if start is not None:
            start = _number(start, f"{where} start")
            timed += 1
            if previous_start is not None and start < previous_start:
                raise InvalidDocument(f"{where} starts before the previous segment (timestamps are out of order)")
            previous_start = start
            end = seg.get("end")
            if end is not None and _number(end, f"{where} end") < start:
                raise InvalidDocument(f"{where} ends before it starts")
        elif seg.get("end") is not None:
            raise InvalidDocument(f"{where} has an end time but no start time")
    if 0 < timed < len(segments):
        raise InvalidDocument("some segments have timestamps and others do not")
    if flag is True and timed == 0:
        raise InvalidDocument("'has_timestamps' is true but no segment has a start time")
    if flag is False and timed:
        raise InvalidDocument("'has_timestamps' is false but segments have start times")
    if flag is None:
        doc["has_timestamps"] = bool(timed)
    return doc


def _reject_constant(name):
    raise ValueError(f"the JSON contains the invalid number {name}")


def load_doc(src: str):
    path = None
    try:
        if Path(src).is_file():
            path = Path(src)
    except (OSError, ValueError):
        pass
    if path is None:
        vid = extract_video_id(src)
        if vid:
            candidate = store_file(f"{vid}.json")
            if candidate.is_file():
                path = candidate
    if path is None:
        die(f"No saved transcript for {src!r}. Run 'fetch' (or 'parse' for pasted text) first.", EXIT_BAD_INPUT)
    try:
        size = path.stat().st_size
        if size > MAX_DOC_BYTES:
            die(f"{path.name} is too large to open ({size // (1024 * 1024)} MB; the limit is "
                f"{MAX_DOC_BYTES // (1024 * 1024)} MB).", EXIT_BAD_INPUT)
        doc = validate_doc(json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=_reject_constant))
    except (ValueError, RecursionError) as exc:  # InvalidDocument, JSONDecodeError, UnicodeDecodeError, ...
        if isinstance(exc, json.JSONDecodeError):
            detail = "it is not valid JSON"
        elif isinstance(exc, UnicodeDecodeError):
            detail = "it is not valid UTF-8 text"
        elif isinstance(exc, RecursionError):
            detail = "its JSON is nested too deeply"
        else:  # InvalidDocument, or the invalid-number ValueError raised by _reject_constant
            detail = str(exc)
        die(f"{path.name} is not a saved transcript: {detail}. "
            "Use 'parse' to import a pasted transcript or caption file.", EXIT_BAD_INPUT)
    except OSError as exc:
        die(f"Cannot read {path.name} ({type(exc).__name__}).", EXIT_BAD_INPUT)
    if path.parent == default_dir():  # reading a stored transcript counts as use, so cleanup keeps it
        try:
            os.utime(path)
        except OSError as exc:
            log.debug("event=touch_failed error=%s", type(exc).__name__)
    return doc, path


def seg_pos(doc, index):
    """Position of a segment: its start time, or its line number when there are no timestamps."""
    start = doc["segments"][index].get("start")
    return start if doc.get("has_timestamps") and start is not None else float(index)


def seg_label(doc, index):
    if doc.get("has_timestamps"):
        return "[" + fmt_time(doc["segments"][index]["start"]) + "]"
    return f"#{index}"


def describe(doc, path):
    segs = doc["segments"]
    words = sum(len(s["text"].split()) for s in segs)
    chars = sum(len(s["text"]) + 9 for s in segs)
    lang = doc.get("language") or "unknown"
    if doc.get("is_generated") is True:
        lang += " (auto-generated captions)"
    elif doc.get("is_generated") is False:
        lang += " (manually created captions)"
    lines = [
        f"video_id: {doc.get('video_id') or '(unknown - no video link available)'}",
        f"url: {doc.get('url') or '(none)'}",
    ]
    if doc.get("title"):
        lines.append(f"title: {doc['title']}")
    if doc.get("channel"):
        lines.append(f"channel: {doc['channel']}")
    lines += [
        f"language: {lang}",
        f"source: {doc.get('source')}",
        f"segments: {len(segs)}",
        f"duration: {fmt_time(doc['duration_s']) if doc.get('duration_s') is not None else 'unknown'}",
        f"words: {words}",
        f"timestamps: {'yes' if doc.get('has_timestamps') else 'NO - cannot cite times for this transcript'}",
        f"read_windows: about {max(1, -(-chars // 30000))} window(s) of 30000 characters",
        f"saved: {path}",
    ]
    if doc.get("video_id") and doc.get("has_timestamps"):
        lines.append(f"link_format: https://youtu.be/{doc['video_id']}?t=<seconds>  (use the 'link' command)")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# parsing pasted transcripts / caption files
# --------------------------------------------------------------------------
def clock_seconds(ts: str):
    """Seconds for a caption/panel timestamp, or None when minutes or seconds are out of range (e.g. 00:75:99)."""
    nums = [float(p) for p in ts.strip().replace(",", ".").split(":")]
    if len(nums) == 3 and (nums[1] >= 60 or nums[2] >= 60):
        return None
    if len(nums) == 2 and nums[1] >= 60:
        return None
    return to_seconds(ts)


def _rolling_overlap(previous, block):
    """Number of leading lines of ``block`` that repeat the trailing lines of ``previous``."""
    for k in range(min(len(previous), len(block)), 0, -1):
        if previous[-k:] == block[:k]:
            return k
    return 0


def parse_cues(text: str, warnings=None):
    """SRT and WebVTT. Drops the repeats YouTube's rolling auto-captions produce, and nothing else.

    A line is treated as a rolling repeat only when the previous cue and this one are continuous in time
    (this cue starts within ROLLING_GAP_S of the previous end) and at least one of them has two or more
    lines, which is the shape of a rolling window. A near-zero-length cue that only repeats text already
    shown is dropped too. Two separate one-line cues with the same words ("No." ... "No.") are legitimate
    repeated speech and are kept. Malformed cues are skipped and counted in ``warnings`` (a list), never
    silently.
    """
    lines = split_lines(text)
    segments, skipped, adjusted = [], 0, 0
    prev_block, prev_end, i, n = None, None, 0, len(lines)
    while i < n:
        m = CUE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        start, end = clock_seconds(m.group(1)), clock_seconds(m.group(2))
        i += 1
        block = []
        while i < n and not CUE_RE.match(lines[i]):
            line = lines[i]
            if not line.strip():
                if line == "" or block:
                    break  # a blank line (or padding after the text) ends the cue
                i += 1  # YouTube's rolling captions begin a cue with a whitespace-only line; skip it
                continue
            if CUE_ID_RE.match(line) and i + 1 < n and CUE_RE.match(lines[i + 1]):
                break  # the next cue's numeric identifier, when blank separator lines are missing
            block.append(clean_text(line, cue=True))
            i += 1
        if start is None or end is None or start > MAX_SECONDS or end > MAX_SECONDS:
            skipped += 1
            continue
        if end < start:
            end, adjusted = start, adjusted + 1
        block = [b for b in block if b]
        if not block:
            continue
        fresh = block
        if prev_block is not None:
            if end - start <= FILLER_MAX_S and block == prev_block[-len(block):]:
                fresh = []
            elif start <= prev_end + ROLLING_GAP_S and (len(prev_block) >= 2 or len(block) >= 2):
                fresh = block[_rolling_overlap(prev_block, block):]
        prev_block, prev_end = block, end
        if not fresh:
            continue
        text_out = " ".join(fresh)
        if segments and segments[-1]["start"] == start and segments[-1]["text"] == text_out:
            continue  # exact duplicate cue
        segments.append({"start": start, "end": end, "text": text_out})
    if any(b["start"] < a["start"] for a, b in zip(segments, segments[1:])):
        segments.sort(key=lambda seg: seg["start"])  # stable: equal starts keep their file order
        adjusted += 1
    if warnings is not None:
        if skipped:
            warnings.append(f"Skipped {skipped} caption cue(s) with malformed timestamps.")
        if adjusted:
            warnings.append("Some caption cues were out of order or ended before they started; "
                            "they were re-ordered or corrected.")
    return segments


def parse_panel(lines):
    """Text copied from YouTube's 'Show transcript' panel: timestamp, then text."""
    segments, start, buf = [], None, []

    def flush():
        if start is not None:
            text = clean_text(" ".join(buf))
            if text:
                segments.append({"start": start, "text": text})

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        m = PANEL_RE.match(line)
        t = clock_seconds(m.group(1)) if m else None
        if t is not None and t <= MAX_SECONDS:
            if start is None or t >= start:  # a time that jumps backwards is just text
                flush()
                start = t
                buf = [m.group(2)] if m.group(2).strip() else []
                continue
        if start is not None:  # header lines before the first timestamp are ignored
            buf.append(line)
    flush()
    return segments


def parse_plain(text: str):
    lines = [clean_text(line) for line in split_lines(text)]
    lines = [line for line in lines if line]
    if len(lines) == 1 and len(lines[0]) > 1200:
        chunks, cur = [], ""
        for sentence in re.split(r"(?<=[.!?])\s+", lines[0]):
            if cur and len(cur) + len(sentence) + 1 > 800:
                chunks.append(cur)
                cur = sentence
            else:
                cur = (cur + " " + sentence).strip()
        if cur:
            chunks.append(cur)
        lines = chunks
    return [{"start": None, "text": line} for line in lines]


def parse_transcript_text(text: str, warnings=None):
    text = text.lstrip("\ufeff")
    lines = split_lines(text)
    if any(CUE_RE.match(line) for line in lines):
        segs = parse_cues(text, warnings)
        if segs:
            return segs, "caption file (srt/vtt)"
    if sum(1 for line in lines if PANEL_RE.match(line)) >= 2:
        segs = parse_panel(lines)
        if segs:
            return segs, "pasted transcript (with timestamps)"
    return parse_plain(text), "pasted text (no timestamps)"


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------
def classify_error(exc):
    name = type(exc).__name__
    text = str(exc).strip()
    short = text[:700] + ("..." if len(text) > 700 else "")
    try:
        import requests
        is_net = isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))
    except ImportError:  # requests normally ships with youtube-transcript-api; fall back to builtins
        is_net = isinstance(exc, (ConnectionError, TimeoutError))
    if is_net:
        return EXIT_NETWORK, ("Cannot reach YouTube from this environment (no internet access, or YouTube "
                              f"is not on this environment's allowed list). Details: {short}")
    if name in ("RequestBlocked", "IpBlocked", "PoTokenRequired", "YouTubeRequestFailed"):
        return EXIT_BLOCKED, (f"YouTube refused the request ({name}). This usually happens from cloud, VPN "
                              f"or shared IP addresses. Details: {short}")
    if name in ("TranscriptsDisabled", "NoTranscriptFound", "VideoUnavailable", "VideoUnplayable",
                "AgeRestricted", "InvalidVideoId"):
        return EXIT_NO_TRANSCRIPT, f"No transcript could be read for this video ({name}). Details: {short}"
    return EXIT_ERROR, f"Unexpected error ({name}): {short}"


def pick_transcript(tlist, langs):
    prefs = [code for code in langs if code.lower() != "any"]
    allow_any = len(prefs) != len(langs)
    if prefs:
        try:
            return tlist.find_transcript(prefs)
        except Exception as exc:  # only "not found" is expected here
            if type(exc).__name__ != "NoTranscriptFound":
                raise
    if allow_any:
        for transcript in tlist:  # manually created transcripts come first
            return transcript
    return None


def try_oembed(video_id):
    """Best-effort title/channel lookup from YouTube's public oEmbed endpoint. Never fails the command."""
    import http.client
    import urllib.request
    target = "https://www.youtube.com/oembed?format=json&url=" + quote(
        f"https://www.youtube.com/watch?v={video_id}", safe="")
    try:
        # The URL is a fixed https address built from a validated 11-character video ID.
        with urllib.request.urlopen(target, timeout=6) as resp:  # noqa: S310
            data = json.loads(resp.read(65536).decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException) as exc:
        log.debug("event=oembed_unavailable error=%s", type(exc).__name__)
        return None, None
    if not isinstance(data, dict):
        return None, None
    title, channel = data.get("title"), data.get("author_name")
    return (title[:300] if isinstance(title, str) else None,
            channel[:300] if isinstance(channel, str) else None)


def requirements_hint() -> str:
    """Path of the plugin's requirements.txt (two folders above this skill), if it can be found."""
    candidate = Path(__file__).resolve().parents[3] / "requirements.txt"
    return f'"{candidate}"' if candidate.is_file() else "requirements.txt"


def segments_from_raw(raw):
    """Turn youtube-transcript-api's raw entries into segments, skipping (and counting) unusable ones."""
    segments, skipped = [], 0
    for item in raw:
        try:
            text = clean_text(item.get("text", "")) if isinstance(item.get("text", ""), str) else ""
            if not text:
                continue
            start = float(item["start"])
            duration = float(item.get("duration") or 0)
        except (AttributeError, KeyError, TypeError, ValueError):
            skipped += 1
            continue
        if not (math.isfinite(start) and math.isfinite(duration)) or not 0 <= start <= MAX_SECONDS or duration < 0:
            skipped += 1
            continue
        segments.append({"start": start, "end": min(start + duration, float(MAX_SECONDS)), "text": text})
    if any(b["start"] < a["start"] for a, b in zip(segments, segments[1:])):
        segments.sort(key=lambda seg: seg["start"])
    return segments, skipped


def cmd_fetch(a):
    vid = extract_video_id(a.video)
    if not vid:
        die(f"Could not find a YouTube video ID in {a.video!r}.", EXIT_BAD_INPUT)
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        die("The 'youtube-transcript-api' package is not installed. Create and activate a virtual environment, "
            f"then run: python -m pip install -r {requirements_hint()} (see README.md for the exact steps).",
            EXIT_MISSING_DEP)
    if not (hasattr(YouTubeTranscriptApi, "fetch") and hasattr(YouTubeTranscriptApi, "list")):
        die("The installed youtube-transcript-api is too old (version 1.0 or newer is needed). "
            f"Inside your virtual environment, run: python -m pip install -U -r {requirements_hint()}",
            EXIT_MISSING_DEP)

    langs = [code.strip() for code in a.lang.split(",") if code.strip()] or ["en", "any"]
    try:
        tlist = YouTubeTranscriptApi().list(vid)
        chosen = pick_transcript(tlist, langs)
        if chosen is None:
            available = ", ".join(
                f"{t.language_code} ({'auto-generated' if t.is_generated else 'manual'})" for t in tlist)
            die(f"No transcript in the requested language(s) {langs}. Available: {available or 'none'}. "
                "Re-run with --lang <code>, or --lang any.", EXIT_NO_TRANSCRIPT)
        raw = chosen.fetch().to_raw_data()
        language, generated = chosen.language_code, bool(chosen.is_generated)
    except Exception as exc:
        code, message = classify_error(exc)
        die(message, code)

    segments, skipped = segments_from_raw(raw)
    if skipped:
        log.warning("Skipped %d caption entr%s that had missing or invalid timing.", skipped,
                    "y" if skipped == 1 else "ies")
    if not segments:
        die("YouTube returned an empty transcript for this video.", EXIT_NO_TRANSCRIPT)
    if len(segments) > MAX_SEGMENTS:
        die(f"This transcript has more than {MAX_SEGMENTS} segments, which is more than this tool will save.",
            EXIT_BAD_INPUT)

    title, channel = try_oembed(vid)
    doc = make_doc(segments, source="youtube-transcript-api", video_id=vid, language=language,
                   is_generated=generated, title=title, channel=channel)
    print(describe(doc, save_doc(doc, a.out)))
    return 0


def read_input_text(file_arg: str) -> str:
    """Read the text to import from a file or stdin, refusing non-files and oversized input."""
    if file_arg == "-":
        text = sys.stdin.read(MAX_INPUT_BYTES + 1)
        if len(text) > MAX_INPUT_BYTES:
            die(f"The input is too large (the limit is {MAX_INPUT_BYTES // (1024 * 1024)} MB).", EXIT_BAD_INPUT)
        return text
    try:
        path = Path(file_arg)
        if not path.is_file():  # also rejects folders, devices and pipes, which could block or never end
            die(f"Cannot read {path.name or file_arg!r}: it is not an existing regular file.", EXIT_BAD_INPUT)
        with open(path, "rb") as handle:
            data = handle.read(MAX_INPUT_BYTES + 1)
    except (OSError, ValueError) as exc:
        die(f"Cannot read {file_arg!r} ({type(exc).__name__}).", EXIT_BAD_INPUT)
    if len(data) > MAX_INPUT_BYTES:
        die(f"{path.name} is too large (the limit is {MAX_INPUT_BYTES // (1024 * 1024)} MB).", EXIT_BAD_INPUT)
    return data.decode("utf-8-sig", errors="replace")


def cmd_parse(a):
    text = read_input_text(a.file)
    if not text.strip():
        die("The transcript text is empty.", EXIT_BAD_INPUT)
    vid = None
    if a.video:
        vid = extract_video_id(a.video)
        if not vid:
            die(f"Could not find a YouTube video ID in {a.video!r}.", EXIT_BAD_INPUT)
    warnings = []
    segments, source = parse_transcript_text(text, warnings)
    for message in warnings:
        log.warning("%s", message)
    if not segments:
        die("Could not find any transcript text in that input.", EXIT_BAD_INPUT)
    if len(segments) > MAX_SEGMENTS:
        die(f"That input has more than {MAX_SEGMENTS} segments, which is more than this tool will save.",
            EXIT_BAD_INPUT)
    doc = make_doc(segments, source=source, video_id=vid, title=a.title)
    print(describe(doc, save_doc(doc, a.out)))
    return 0


def cmd_cleanup(a):
    store = ensure_store(create=False)
    if not store.is_dir():
        print("Nothing to clean: the transcript store does not exist yet.")
        return 0
    if custom_storage() and not (store / STORE_MARKER).is_file():
        print("Cleanup skipped: the folder in TUBEGPT_STORAGE_DIR was not created by tubeGPT (it has no "
              f"{STORE_MARKER} marker file), so none of its files were touched.")
        return 0
    result = cleanup_store(store, dry_run=a.dry_run)
    verb = "Would remove" if a.dry_run else "Removed"
    print(f"{verb} {result['expired']} expired and {result['over_limit']} surplus transcript file(s) "
          f"({result['freed_bytes']} bytes). {result['remaining']} file(s) remain.")
    return 0


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------
def cmd_show(a):
    doc, path = load_doc(a.src)
    if a.stats:
        print(describe(doc, path))
        return 0
    lo = parse_time_arg(a.t_from) if a.t_from is not None else None
    hi = parse_time_arg(a.t_to) if a.t_to is not None else None
    chosen = [i for i in range(len(doc["segments"]))
              if (lo is None or seg_pos(doc, i) >= lo) and (hi is None or seg_pos(doc, i) < hi)]
    if not chosen:
        print("(no transcript lines in that range)")
        return 0
    used, printed = 0, 0
    for i in chosen:
        line = f"{seg_label(doc, i)} {doc['segments'][i]['text']}"
        if printed and used + len(line) + 1 > a.max_chars:
            nxt = seg_pos(doc, i)
            nxt_arg = f"{int(nxt * 1000) / 1000:.3f}" if doc.get("has_timestamps") else str(i)
            print(f"[... stopped after {printed} of {len(chosen)} lines in this range. "
                  f"Continue with: show {path} --from {nxt_arg}]")
            return 0
        print(line)
        used += len(line) + 1
        printed += 1
    return 0


def cmd_search(a):
    terms = [re.sub(r"\s+", " ", t.strip().lower()) for t in a.terms]
    terms = [t for t in terms if t]
    if not terms:
        die("Give at least one non-empty search term.", EXIT_BAD_INPUT)
    if len(terms) > MAX_TERMS:
        die(f"Too many search terms ({len(terms)}); the limit is {MAX_TERMS}.", EXIT_BAD_INPUT)
    doc, _ = load_doc(a.src)
    segs = doc["segments"]
    lowered = [s["text"].lower() for s in segs]
    offsets, pos = [], 0
    for t in lowered:
        offsets.append(pos)
        pos += len(t) + 1
    full = " ".join(lowered)

    hits = set()
    for needle in terms:
        k = full.find(needle)
        while k >= 0:
            hits.add(bisect.bisect_right(offsets, k) - 1)
            k = full.find(needle, k + 1)
    if not hits:
        print("No matches for: " + ", ".join(repr(t) for t in a.terms) +
              "\n(Try other wording or synonyms. Auto-generated captions can also misspell names and terms.)")
        return 0

    reported, last = 0, None
    for idx in sorted(hits):
        if last is not None and idx - last <= a.context:
            continue  # already inside the previous hit's context window
        if reported >= a.max:
            print(f"[... more matches exist; showing the first {a.max}. Narrow the terms or raise --max.]")
            break
        window = " ".join(s["text"] for s in segs[max(0, idx - a.context): idx + a.context + 1])
        where = seg_label(doc, idx)
        if doc.get("has_timestamps") and doc.get("video_id"):
            where += " " + link_url(doc["video_id"], segs[idx]["start"])
        print(f"{where}\n    {window}")
        reported += 1
        last = idx
    return 0


def cmd_link(a):
    doc, _ = load_doc(a.src)
    if not doc.get("has_timestamps"):
        die("This transcript has no timestamps, so there is nothing to link to.", EXIT_BAD_INPUT)
    if len(a.times) > MAX_LINK_TIMES:
        die(f"Too many times ({len(a.times)}); the limit is {MAX_LINK_TIMES} per call.", EXIT_BAD_INPUT)
    parsed = [(value, int(parse_time_arg(value))) for value in a.times]  # validate all before printing any
    for value, seconds in parsed:
        label = fmt_time(seconds)
        if doc.get("video_id"):
            print(f"{value} => [{label}]({link_url(doc['video_id'], seconds)})")
        else:
            print(f"{value} => [{label}]  (no video URL known, so no link)")
    return 0


# --------------------------------------------------------------------------
def bounded_int(lo: int, hi: int):
    """argparse type: a plain integer within [lo, hi], with an understandable message otherwise."""
    def convert(text: str) -> int:
        if not re.fullmatch(r"[+-]?[0-9]+", text.strip()):
            raise argparse.ArgumentTypeError(f"{text!r} is not a whole number")
        value = int(text)
        if value < 0 <= lo:
            raise argparse.ArgumentTypeError(f"cannot be negative (got {value}); allowed range is {lo} to {hi}")
        if not lo <= value <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo} and {hi} (got {value})")
        return value
    return convert


LANG_CODE_RE = re.compile(r"^(?:any|[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*)$")


def language_list(text: str) -> str:
    codes = [c.strip() for c in text.split(",") if c.strip()]
    if len(codes) > MAX_LANG_CODES:
        raise argparse.ArgumentTypeError(f"give at most {MAX_LANG_CODES} language codes")
    for code in codes:
        if not LANG_CODE_RE.match(code):
            raise argparse.ArgumentTypeError(f"{code!r} is not a language code such as en, hi or pt-BR (or 'any')")
    return text


def search_term(text: str) -> str:
    if len(text) > MAX_TERM_CHARS:
        raise argparse.ArgumentTypeError(f"a search term can have at most {MAX_TERM_CHARS} characters")
    return text


def short_text(limit: int):
    def convert(text: str) -> str:
        if len(text) > limit:
            raise argparse.ArgumentTypeError(f"must have at most {limit} characters")
        return text
    return convert


def nonempty_path(text: str) -> str:
    if not text.strip() or "\0" in text:
        raise argparse.ArgumentTypeError("must be a non-empty file path")
    return text


def build_parser():
    p = argparse.ArgumentParser(description="Timestamped YouTube transcripts for the youtube-conversation skill.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print structured debug logs to stderr (never transcript text)")
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="download a video's transcript")
    f.add_argument("video", type=short_text(2048), help="YouTube URL or 11-character video ID")
    f.add_argument("--lang", default="en,any", type=language_list,
                   help="preferred language codes in priority order; 'any' = fall back to whatever exists "
                        "(default: en,any)")
    f.add_argument("--out", type=nonempty_path, help="where to save the JSON (default: temp folder, named by video ID)")
    f.set_defaults(func=cmd_fetch)

    pa = sub.add_parser("parse", help="import a pasted transcript or .srt/.vtt/.txt file")
    pa.add_argument("file", type=nonempty_path, help="path to the text file, or - for stdin")
    pa.add_argument("--video", type=short_text(2048), help="YouTube URL or ID, so timestamps can become links")
    pa.add_argument("--title", type=short_text(300), help="optional title label")
    pa.add_argument("--out", type=nonempty_path, help="where to save the JSON")
    pa.set_defaults(func=cmd_parse)

    s = sub.add_parser("show", help="print the transcript with timestamps")
    s.add_argument("src", type=short_text(2048))
    s.add_argument("--stats", action="store_true", help="print details only, not the text")
    s.add_argument("--from", dest="t_from", help="start time, inclusive (754, 12:34, 1:02:03)")
    s.add_argument("--to", dest="t_to", help="end time, exclusive")
    s.add_argument("--max-chars", type=bounded_int(1, 1_000_000), default=30000,
                   help="stop after about this many characters (1-1000000, default 30000)")
    s.set_defaults(func=cmd_show)

    se = sub.add_parser("search", help="find where terms are said")
    se.add_argument("src", type=short_text(2048))
    se.add_argument("terms", nargs="+", type=search_term,
                    help=f"terms or quoted phrases; a match on any of them counts (at most {MAX_TERMS})")
    se.add_argument("--context", type=bounded_int(0, 50), default=2,
                    help="neighbouring lines to show on each side (0-50, default 2)")
    se.add_argument("--max", type=bounded_int(1, 1000), default=15,
                    help="maximum places to show (1-1000, default 15)")
    se.set_defaults(func=cmd_search)

    li = sub.add_parser("link", help="make markdown timestamp links")
    li.add_argument("src", type=short_text(2048))
    li.add_argument("times", nargs="+", help=f"timestamps such as 754, 12:34 or 1:02:03 (at most {MAX_LINK_TIMES})")
    li.set_defaults(func=cmd_link)

    cl = sub.add_parser("cleanup", help="remove expired or surplus saved transcripts from the default store")
    cl.add_argument("--dry-run", action="store_true", help="report what would be removed without deleting anything")
    cl.set_defaults(func=cmd_cleanup)
    return p


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever sys.stderr is at the moment of each record (keeps tests and redirects working)."""

    def __init__(self):
        super().__init__(sys.stderr)

    def emit(self, record):
        self.stream = sys.stderr
        super().emit(record)


def setup_logging(verbose: bool):
    for handler in list(log.handlers):
        log.removeHandler(handler)
    handler = _StderrHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG if verbose else logging.WARNING)
    log.propagate = False


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # not a text stream that supports reconfigure (for example a test capture object)
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        die("Interrupted.", 130)
    except BrokenPipeError:
        # The reader (for example `| head`) closed early; this is not an error in the transcript itself.
        try:
            sys.stdout = open(os.devnull, "w")  # noqa: SIM115
        except OSError:
            pass
        return EXIT_ERROR
    except Exception as exc:  # last resort: every known failure above exits through die() with its own code
        log.debug("event=unexpected_error", exc_info=True)
        die(f"Unexpected internal error ({type(exc).__name__}). Re-run with --verbose for details.", EXIT_ERROR)


if __name__ == "__main__":
    sys.exit(main())
