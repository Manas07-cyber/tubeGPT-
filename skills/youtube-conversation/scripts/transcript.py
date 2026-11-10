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
touched by cleanup. In a folder you chose with TUBEGPT_STORAGE_DIR nothing is ever deleted unless you also
set TUBEGPT_STORAGE_CLEANUP=1 (and the folder carries tubeGPT's marker file).

Exit codes: 0 ok | 1 unexpected error | 2 bad input | 3 cannot reach YouTube |
            4 no readable transcript for this video | 5 YouTube refused the
            request (blocked / rate limited) | 6 missing or outdated dependency |
            7 transcript storage unavailable, not writable or full
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import errno
import hashlib
import html
import itertools
import json
import logging
import math
import os
import re
import stat
import sys
import tempfile
import time
import traceback
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

if sys.version_info < (3, 11):  # a clear message instead of a traceback from a newer language feature
    sys.exit(f"tubeGPT needs Python 3.11 or newer; this is Python {sys.version_info[0]}.{sys.version_info[1]}. "
             "Install a current Python (see README.md, Developer setup) and run the command again.")

try:  # POSIX advisory file locks
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:  # Windows byte-range locks
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

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
# Anything that looks like a caption timing line, even a malformed one ("00:99:99.000 --> ...", "1 --> 2").
TIMING_LINE_RE = re.compile(r"^\s*[0-9:.,;]+\s*-->\s*[0-9:.,;]*")
# Control characters other than whitespace never belong in speech; they could drive a terminal if printed.
CONTROL_RE = re.compile(r"[\x00-\x08\x0e-\x1b\x7f-\x9f]")
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
MAX_ROLLING_LINES = 8  # a rolling window is 2-3 lines; comparing more would only invite quadratic work

# Storage defaults, all overridable through environment variables (see module docstring).
STORE_MARKER = ".tubegpt-store"
MARKER_TEXT = ("tubeGPT transcript store v1\n"
               "Files in this folder whose names match tubeGPT's own naming may be removed by 'cleanup' when\n"
               "TUBEGPT_STORAGE_CLEANUP=1 is set. This file is a safety guard, not proof of ownership.\n")
MARKER_PREFIX = "tubeGPT transcript store"
LOCK_NAME = ".tubegpt-lock"
LOCK_TIMEOUT_S = 10.0
# <video id>.json, or pasted-<hash>.json (32 hex characters from SHA-256; 8 characters were used by older versions)
STORE_FILE_RE = re.compile(r"^(?:[A-Za-z0-9_-]{11}|pasted-(?:[0-9a-f]{8}|[0-9a-f]{32}))\.json$")
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
    text = value.strip().replace(",", ".") if isinstance(value, str) and len(value) <= 64 else ""
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
    text = CONTROL_RE.sub("", text)
    # Zero-width space and BOM are noise; zero-width joiners are kept because Indic scripts need them.
    text = text.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def split_lines(text: str):
    """Split on real line breaks only (str.splitlines also splits on U+2028, form feeds and others)."""
    return re.split(r"\r\n|\r|\n", text)


def split_text(text: str, limit: int | None = None):
    """Split ``text`` into pieces of at most ``limit`` characters (default MAX_SEGMENT_CHARS), preferring a space.

    The default is looked up on each call, so it always matches the limit the loader enforces.
    """
    if limit is None:
        limit = MAX_SEGMENT_CHARS
    if len(text) <= limit:
        return [text]
    pieces, start = [], 0
    while len(text) - start > limit:
        cut = text.rfind(" ", start + limit // 2, start + limit + 1)
        if cut == -1:  # no space in the window (for example unspaced CJK text): break at the limit
            piece, start = text[start:start + limit], start + limit
        else:
            piece, start = text[start:cut], cut + 1
        if piece:
            pieces.append(piece)
    if text[start:]:
        pieces.append(text[start:])
    return pieces


def enforce_segment_limit(segments):
    """Split any segment longer than MAX_SEGMENT_CHARS (same start/end for each piece). Returns (segments, count)."""
    out, split = [], 0
    for seg in segments:
        if len(seg["text"]) <= MAX_SEGMENT_CHARS:
            out.append(seg)
            continue
        split += 1
        for piece in split_text(seg["text"]):
            out.append({**seg, "text": piece})
    return out, split


def make_doc(segments, *, source, video_id=None, language=None, is_generated=None,
             title=None, channel=None):
    segments, split = enforce_segment_limit(segments)
    if split:
        log.warning("Split %d very long caption segment(s) into smaller pieces so they can be saved and read.", split)
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
_IS_POSIX = os.name == "posix"
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)  # Windows only: no newline translation


class UnsafeStoreEntry(Exception):
    """A store entry that is a symbolic link, not a regular file, or owned by another user."""


def default_dir() -> Path:
    custom = os.environ.get("TUBEGPT_STORAGE_DIR", "").strip()
    if custom:
        return Path(custom).expanduser()
    return Path(tempfile.gettempdir()) / "youtube-conversation"


def custom_storage() -> bool:
    """True when the user pointed the store at their own folder (which may hold unrelated files)."""
    return bool(os.environ.get("TUBEGPT_STORAGE_DIR", "").strip())


def custom_cleanup_enabled() -> bool:
    """Deletion inside a user-chosen folder is opt-in: TUBEGPT_STORAGE_CLEANUP=1."""
    return os.environ.get("TUBEGPT_STORAGE_CLEANUP", "").strip() == "1"


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


def _owned_by_me(info) -> bool:
    """True when the file belongs to the current user (always true where the OS has no user IDs)."""
    return not hasattr(os, "getuid") or info.st_uid == os.getuid()


def _open_regular(path, flags=os.O_RDONLY, mode=0o600):
    """Open an existing file without following a symbolic link; verify it is a regular file we own.

    Returns (fd, fstat result). Raises UnsafeStoreEntry for links, special files and other users' files.
    """
    if not _IS_POSIX and os.path.islink(path):  # Windows has no O_NOFOLLOW
        raise UnsafeStoreEntry("symbolic link")
    try:
        fd = os.open(path, flags | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC | _O_BINARY, mode)
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # O_NOFOLLOW met a symbolic link
            raise UnsafeStoreEntry("symbolic link") from exc
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeStoreEntry("not a regular file")
        if not _owned_by_me(info):
            raise UnsafeStoreEntry("owned by another user")
    except BaseException:
        os.close(fd)
        raise
    return fd, info


def marker_is_valid(store) -> bool:
    """True if the store folder holds a real marker file: a regular, non-link file we own with our header.

    This is a guard against deleting inside a folder that was never meant as a transcript store. It is NOT
    proof of ownership: anyone who can write to the folder can create the file.
    """
    try:
        fd, info = _open_regular(Path(store) / STORE_MARKER)
    except (OSError, UnsafeStoreEntry):
        return False
    with os.fdopen(fd, "rb") as handle:
        head = handle.read(256)
    return info.st_size < 4096 and head.decode("utf-8", errors="replace").startswith(MARKER_PREFIX)


def _write_marker(store):
    """Create the marker atomically and without following links; an existing marker is left alone."""
    try:
        fd = os.open(Path(store) / STORE_MARKER,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as handle:
        handle.write(MARKER_TEXT.encode("utf-8"))


def _tighten_dir(store):
    """Make the store folder owner-only (0700) through a descriptor, so a swapped-in link is never followed."""
    if not _IS_POSIX:
        return
    fd = os.open(store, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not _owned_by_me(info):
            die("The transcript storage folder belongs to another user, so it was not used.", EXIT_STORAGE)
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def secure_store_files(store):
    """Make tubeGPT's own transcript files owner-only (0600). Other files in the folder are never touched.

    Only regular files with tubeGPT's generated names, owned by the current user and not reached through a
    symbolic link are changed. Called only for folders that are ours (the default store or a marked one).
    """
    if not _IS_POSIX:
        return 0
    changed = 0
    try:
        names = os.listdir(store)
    except OSError as exc:
        storage_error(exc, "check the permissions of saved transcripts")
    for name in names:
        if not (STORE_FILE_RE.match(name) or TMP_FILE_RE.match(name)):
            continue
        try:
            fd, info = _open_regular(Path(store) / name)
        except UnsafeStoreEntry:
            log.debug("event=permission_check_skipped reason=unsafe_entry")
            continue
        except FileNotFoundError:
            continue  # removed by another process meanwhile
        except OSError as exc:
            storage_error(exc, "check the permissions of saved transcripts")
        try:
            if stat.S_IMODE(info.st_mode) & 0o077:
                os.fchmod(fd, 0o600)
                changed += 1
        except OSError as exc:
            storage_error(exc, "restrict the permissions of saved transcripts")
        finally:
            os.close(fd)
    return changed


def ensure_store(create: bool = True) -> Path:
    """Return the transcript store folder, creating it privately and refusing unsafe ones.

    Both the default and a user-chosen folder are refused if the final path component is a symbolic link, is
    not a folder, or belongs to another user. On POSIX a folder that is ours (the default store, or a custom
    one carrying tubeGPT's marker) is tightened to owner-only 0700 and its transcript files to 0600; a custom
    folder that is not ours is never re-permissioned. A marker is written only when we created the custom folder
    or found it empty.
    """
    store = default_dir()
    custom = custom_storage()
    try:
        created = False
        try:
            info = os.lstat(store)
        except FileNotFoundError:
            if not create:
                return store
            try:
                store.mkdir(parents=True, mode=0o700)
                created = True
            except FileExistsError:
                pass  # somebody else created it first: validated like any existing folder below
            info = os.lstat(store)
        if stat.S_ISLNK(info.st_mode):
            die("The transcript storage folder is a symbolic link, so it was not used. "
                "Point TUBEGPT_STORAGE_DIR at the real folder.", EXIT_STORAGE)
        if not stat.S_ISDIR(info.st_mode):
            die("The transcript storage location exists but is not a folder.", EXIT_STORAGE)
        if not _owned_by_me(info):
            die("The transcript storage folder belongs to another user, so it was not used.", EXIT_STORAGE)
        if custom and create and not marker_is_valid(store) and (created or not any(store.iterdir())):
            _write_marker(store)
        if not custom or marker_is_valid(store):
            _tighten_dir(store)
            secure_store_files(store)
        else:
            log.debug("event=custom_store_permissions_unchanged")
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


def _lock_acquire(fd, shared: bool, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if fcntl is not None:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            elif msvcrt is not None:  # byte-range locks are exclusive only
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                return False
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def _lock_release(fd):
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError as exc:
        log.debug("event=unlock_failed error=%s", type(exc).__name__)


@contextlib.contextmanager
def store_lock(store, *, shared: bool = False, timeout: float = LOCK_TIMEOUT_S):
    """Advisory lock on the store (a lock file inside it) that serialises saves, reads-with-touch and cleanup.

    Yields True when the lock is held and False when it could not be taken within ``timeout`` seconds or
    locking is unavailable. Callers must stay correct without it (cleanup revalidates every file), the lock
    only closes the remaining windows between cooperating tubeGPT processes.
    """
    fd = None
    try:
        if custom_storage() and not marker_is_valid(store):
            raise UnsafeStoreEntry("not a tubeGPT store")  # never create files in an unrelated folder
        fd, _ = _open_regular(Path(store) / LOCK_NAME, os.O_RDWR | os.O_CREAT)
    except (OSError, UnsafeStoreEntry) as exc:
        log.debug("event=lock_unavailable error=%s", type(exc).__name__)
    held = False
    try:
        if fd is not None:
            held = _lock_acquire(fd, shared, timeout)
            if not held:
                log.debug("event=lock_timeout")
        yield held
    finally:
        if fd is not None:
            if held:
                _lock_release(fd)
            os.close(fd)


def write_json_atomic(path, doc):
    """Write JSON through a private temporary file in the same folder, then rename it into place.

    A crash or a full disk never leaves a half-written transcript, and the temporary file is always removed.
    """
    try:
        data = json.dumps(doc, ensure_ascii=False, indent=1, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        die(f"The transcript could not be converted to JSON ({type(exc).__name__}).", EXIT_BAD_INPUT)
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


class _Entry(collections.namedtuple("_Entry", "name mtime size ident is_tmp")):
    """A scanned store file. ``ident`` (device, inode, mtime_ns, size) detects replacement or refresh."""


def _dir_fd(store):
    if not (_IS_POSIX and os.scandir in os.supports_fd and os.stat in os.supports_dir_fd
            and os.unlink in os.supports_dir_fd):
        return None
    fd = os.open(store, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
    if not _owned_by_me(os.fstat(fd)):
        os.close(fd)
        raise UnsafeStoreEntry("owned by another user")
    return fd


def _stat_entry(store, dirfd, name):
    if dirfd is not None:
        return os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    return os.lstat(Path(store) / name)


def _scan_store(store, dirfd):
    entries = []
    with os.scandir(dirfd if dirfd is not None else store) as it:
        for entry in it:
            is_tmp = bool(TMP_FILE_RE.match(entry.name))
            if not (is_tmp or STORE_FILE_RE.match(entry.name)):
                continue
            try:
                info = _stat_entry(store, dirfd, entry.name)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or not _owned_by_me(info):
                continue  # links, folders and other users' files are never candidates
            entries.append(_Entry(entry.name, info.st_mtime, info.st_size,
                                  (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size), is_tmp))
    return entries


def _remove_if_unchanged(store, dirfd, entry, now, min_age):
    """Delete ``entry`` only if it is still exactly the file that was scanned and is still old enough.

    A file that another process replaced, rewrote or touched (a read counts as use) since the scan is kept.
    """
    try:
        info = _stat_entry(store, dirfd, entry.name)
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("Could not re-check an old transcript file (%s).", type(exc).__name__)
        return False
    ident = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
    if not stat.S_ISREG(info.st_mode) or not _owned_by_me(info) or ident != entry.ident:
        log.debug("event=cleanup_skipped_changed")
        return False
    if now - info.st_mtime < min_age:
        return False
    try:
        if dirfd is not None:
            os.unlink(entry.name, dir_fd=dirfd)
        else:
            os.unlink(Path(store) / entry.name)
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("Could not remove an old transcript file (%s).", type(exc).__name__)
        return False
    return True


def cleanup_allowed(store):
    """Return (allowed, reason). Custom folders need TUBEGPT_STORAGE_CLEANUP=1 and a valid marker file."""
    if not custom_storage():
        return True, ""
    if not custom_cleanup_enabled():
        return False, "optin"
    if not marker_is_valid(store):
        return False, "marker"
    return True, ""


def cleanup_store(store, *, keep=(), dry_run: bool = False, now=None):
    """Delete expired and surplus transcripts from the store. Returns a summary dict.

    Safety rules, all covered by tests:
    - in a folder chosen with TUBEGPT_STORAGE_DIR, acts only with TUBEGPT_STORAGE_CLEANUP=1 and a valid marker;
    - only looks at regular files directly inside the store, owned by the current user (no recursion;
      symbolic links and other users' files are ignored);
    - only deletes names this program generates (<video id>.json, pasted-<hash>.json, leftover .tmp-*.json);
    - never deletes a file in ``keep`` or one used (loaded or saved) within the last CLEANUP_GRACE_S seconds;
    - re-checks each file immediately before deleting it and skips any that was replaced or refreshed after
      the scan (callers also hold store_lock so cooperating processes cannot interleave);
    - uses os.unlink on single files only, never recursive deletion.
    """
    store = Path(store)
    result = {"expired": 0, "over_limit": 0, "freed_bytes": 0, "remaining": 0, "dry_run": dry_run}
    allowed, reason = cleanup_allowed(store)
    if not allowed:
        log.debug("event=cleanup_skipped reason=%s", reason)
        return result
    retention_s, max_bytes, max_files = storage_limits()
    now = time.time() if now is None else now
    keep_names = {Path(k).name for k in keep}
    dirfd = None
    try:
        dirfd = _dir_fd(store)
        entries = _scan_store(store, dirfd)
        entries.sort(key=lambda e: (e.mtime, e.name))

        def protected(entry):
            return entry.name in keep_names or now - entry.mtime < CLEANUP_GRACE_S

        removed = set()

        def remove(entry, kind, min_age):
            if not dry_run and not _remove_if_unchanged(store, dirfd, entry, now, min_age):
                return False
            removed.add(entry.name)
            result[kind] += 1
            result["freed_bytes"] += entry.size
            return True

        for entry in entries:
            limit = CLEANUP_GRACE_S if entry.is_tmp else retention_s
            if not protected(entry) and now - entry.mtime > limit:
                remove(entry, "expired", limit)
        live = [e for e in entries if e.name not in removed and not e.is_tmp]
        total, count = sum(e.size for e in live), len(live)
        for entry in live:
            if total <= max_bytes and count <= max_files:
                break
            if not protected(entry) and remove(entry, "over_limit", CLEANUP_GRACE_S):
                total -= entry.size
                count -= 1
        result["remaining"] = count
    except (OSError, UnsafeStoreEntry) as exc:
        log.warning("Could not clean the transcript store (%s).", type(exc).__name__)
        return result
    finally:
        if dirfd is not None:
            os.close(dirfd)
    log.debug("event=cleanup expired=%d over_limit=%d freed_bytes=%d remaining=%d dry_run=%s",
              result["expired"], result["over_limit"], result["freed_bytes"], result["remaining"], dry_run)
    return result


def pasted_name(doc) -> str:
    """Stable store name for a transcript without a video ID: SHA-256 over every timestamp and text."""
    digest = hashlib.sha256()
    for seg in doc["segments"]:
        digest.update(repr(seg.get("start")).encode("ascii"))
        digest.update(b"\0")
        digest.update(seg["text"].encode("utf-8"))
        digest.update(b"\0")
    return "pasted-" + digest.hexdigest()[:32]


def save_doc(doc, out):
    """Validate, then save. A document that this program could not load again is never written."""
    try:
        validate_doc(doc)
    except InvalidDocument as exc:
        die(f"The transcript was not saved because it is not valid: {exc}.", EXIT_BAD_INPUT)
    if out:
        path = Path(out)
        if path.is_dir():
            die("--out must be a file path, but it points to a folder.", EXIT_BAD_INPUT)
        write_json_atomic(path, doc)
        return path
    name = doc.get("video_id") or pasted_name(doc)
    store = ensure_store()
    path = store_file(f"{name}.json")
    with store_lock(store) as locked:
        if locked:
            cleanup_store(store, keep=[path])
        else:
            log.debug("event=cleanup_deferred reason=lock_busy")
        write_json_atomic(path, doc)
    return path


# --------------------------------------------------------------------------
# loading and validating saved transcripts
# --------------------------------------------------------------------------
class InvalidDocument(ValueError):
    """A saved transcript that does not have the structure this tool writes."""


class ParseError(ValueError):
    """Pasted or imported text that looks like a timestamped caption file but contains no readable cues."""


def _number(value, what, *, lo=0.0, hi=float(MAX_SECONDS)):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidDocument(f"{what} must be a number")
    if isinstance(value, float) and not math.isfinite(value):  # (math.isfinite overflows on huge integers)
        raise InvalidDocument(f"{what} must be a finite number")
    if not lo <= value <= hi:  # Python compares integers and floats exactly, so this cannot overflow
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


def _read_validated(fd, info, name):
    """Read and validate a saved transcript from an already opened, already checked file descriptor."""
    if info.st_size > MAX_DOC_BYTES:
        die(f"{name} is too large to open ({info.st_size // (1024 * 1024)} MB; the limit is "
            f"{MAX_DOC_BYTES // (1024 * 1024)} MB).", EXIT_BAD_INPUT)
    with os.fdopen(os.dup(fd), "rb") as handle:
        data = handle.read(MAX_DOC_BYTES + 1)
    if len(data) > MAX_DOC_BYTES:
        die(f"{name} is too large to open (the limit is {MAX_DOC_BYTES // (1024 * 1024)} MB).", EXIT_BAD_INPUT)
    try:
        return validate_doc(json.loads(data.decode("utf-8-sig"), parse_constant=_reject_constant))
    except (ValueError, RecursionError, OverflowError) as exc:  # InvalidDocument, JSONDecodeError, ...
        if isinstance(exc, json.JSONDecodeError):
            detail = "it is not valid JSON"
        elif isinstance(exc, UnicodeDecodeError):
            detail = "it is not valid UTF-8 text"
        elif isinstance(exc, RecursionError):
            detail = "its JSON is nested too deeply"
        elif isinstance(exc, OverflowError):
            detail = "it contains a number that is too large"
        elif "digits" in str(exc) and "integer" in str(exc):  # Python's limit on converting huge integers
            detail = "it contains a number with far too many digits"
        else:  # InvalidDocument, or the invalid-number ValueError raised by _reject_constant
            detail = str(exc)
        die(f"{name} is not a saved transcript: {detail}. "
            "Use 'parse' to import a pasted transcript or caption file.", EXIT_BAD_INPUT)


def _touch(fd, path):
    """Mark a stored transcript as used (resets its retention clock)."""
    try:
        if os.utime in os.supports_fd:
            os.utime(fd)
        else:
            os.utime(path)
    except OSError as exc:
        log.debug("event=touch_failed error=%s", type(exc).__name__)


def _is_store_path(path: Path) -> bool:
    return os.path.dirname(os.path.abspath(path)) == os.path.abspath(default_dir())


def load_doc(src: str):
    """Load a saved transcript by file path, video URL or video ID.

    Anything inside the transcript store goes through the same checks as saving: the store folder must not be a
    link or belong to another user, and a stored file must be a regular file owned by us (links are never
    followed). A file you name explicitly elsewhere is read as you asked.
    """
    path = None
    try:
        if Path(src).is_file():
            path = Path(src)
    except (OSError, ValueError):
        pass
    in_store = path is not None and _is_store_path(path)
    if path is None:
        vid = extract_video_id(src)
        if vid:
            path, in_store = store_file(f"{vid}.json"), True
    if in_store:
        store = ensure_store(create=False)
        if not store.is_dir() or not path.exists() and not path.is_symlink():
            path = None
    if path is None:
        die(f"No saved transcript for {src!r}. Run 'fetch' (or 'parse' for pasted text) first.", EXIT_BAD_INPUT)
    try:
        if in_store:
            with store_lock(store, shared=True):
                try:
                    fd, info = _open_regular(path)
                except UnsafeStoreEntry as exc:
                    die(f"{path.name} was not used because it is not a regular transcript file owned by you "
                        f"({exc}). Symbolic links in the transcript store are never followed.", EXIT_STORAGE)
                try:
                    if _IS_POSIX and stat.S_IMODE(info.st_mode) & 0o077 and (not custom_storage()
                                                                           or marker_is_valid(store)):
                        os.fchmod(fd, 0o600)
                    doc = _read_validated(fd, info, path.name)
                    _touch(fd, path)  # reading counts as use, so cleanup keeps it
                finally:
                    os.close(fd)
        else:
            try:
                fd, info = _open_regular(path)
            except UnsafeStoreEntry:  # a link or special file you named yourself: read what it points to
                fd, info = os.open(path, os.O_RDONLY), os.stat(path)
                if not stat.S_ISREG(info.st_mode):
                    os.close(fd)
                    die(f"Cannot read {path.name}: it is not a regular file.", EXIT_BAD_INPUT)
            try:
                doc = _read_validated(fd, info, path.name)
            finally:
                os.close(fd)
    except OSError as exc:
        die(f"Cannot read {path.name} ({type(exc).__name__}).", EXIT_BAD_INPUT)
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
    for k in range(min(len(previous), len(block), MAX_ROLLING_LINES), 0, -1):
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
    segments, skipped, adjusted, duplicates = [], 0, 0, 0
    prev_block, prev_end, i, n = None, None, 0, len(lines)
    while i < n:
        m = CUE_RE.match(lines[i])
        if not m:
            if TIMING_LINE_RE.match(lines[i]):
                skipped += 1  # a timing line we cannot read: its text lines below are skipped with it
            i += 1
            continue
        start, end = clock_seconds(m.group(1)), clock_seconds(m.group(2))
        i += 1
        block = []
        while i < n and not CUE_RE.match(lines[i]) and not TIMING_LINE_RE.match(lines[i]):
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
            duplicates += 1  # exact duplicate cue
            continue
        segments.append({"start": start, "end": end, "text": text_out})
    if any(b["start"] < a["start"] for a, b in zip(segments, segments[1:], strict=False)):
        segments.sort(key=lambda seg: seg["start"])  # stable: equal starts keep their file order
        adjusted += 1
    if warnings is not None:
        if skipped:
            warnings.append(f"Skipped {skipped} caption cue(s) with malformed timestamps.")
        if duplicates:
            warnings.append(f"Dropped {duplicates} caption cue(s) that exactly repeated the cue before them.")
        if adjusted:
            warnings.append("Some caption cues were out of order or ended before they started; "
                            "they were re-ordered or corrected.")
    return segments


def parse_panel(lines, warnings=None):
    """Text copied from YouTube's 'Show transcript' panel: timestamp, then text."""
    segments, start, buf, ignored = [], None, [], 0

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
        if start is not None:
            buf.append(line)
        else:
            ignored += 1  # header lines before the first timestamp are not transcript text
    flush()
    if warnings is not None and ignored:
        warnings.append(f"Ignored {ignored} line(s) before the first timestamp (headings or page text).")
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
    timing_lines = sum(1 for line in lines if TIMING_LINE_RE.match(line))
    if timing_lines:
        segs = parse_cues(text, warnings)
        if segs:
            return segs, "caption file (srt/vtt)"
        raise ParseError(f"The text has {timing_lines} caption timing line(s) (\"-->\") but none could be read, "
                         "so it was not imported as plain text. Check the timestamps, for example "
                         "00:01:02.500 --> 00:01:04.000.")
    panel_lines = sum(1 for line in lines if PANEL_RE.match(line))
    if panel_lines >= 2:
        segs = parse_panel(lines, warnings)
        if segs:
            return segs, "pasted transcript (with timestamps)"
        raise ParseError(f"The text has {panel_lines} lines that start like timestamps but none could be read, so "
                         "it was not imported as plain text. Check them, for example 12:34 or 1:02:03 "
                         "(minutes and seconds must be below 60).")
    return parse_plain(text), "pasted text (no timestamps)"


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------
def scrub(text: str, limit: int = 700) -> str:
    """Collapse whitespace, hide this machine's home, working and temp folders, and cap the length."""
    text = re.sub(r"\s+", " ", str(text)).strip()
    folders = {str(Path.home()), tempfile.gettempdir()}
    try:
        folders.add(os.getcwd())
    except OSError:
        pass
    for folder in sorted(folders, key=len, reverse=True):
        if len(folder) > 1:
            text = text.replace(folder, "<folder>")
    return text[:limit] + ("..." if len(text) > limit else "")


def classify_error(exc):
    name = type(exc).__name__
    short = scrub(exc)
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
    log.debug("event=fetch_error type=%s detail=%s", name, short)
    return EXIT_ERROR, f"Unexpected error while fetching the transcript ({name}). Re-run with -v for details."


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
    return (clean_text(title)[:300] if isinstance(title, str) else None,
            clean_text(channel)[:300] if isinstance(channel, str) else None)


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
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):  # OverflowError: huge integers
            skipped += 1
            continue
        if not (math.isfinite(start) and math.isfinite(duration)) or not 0 <= start <= MAX_SECONDS or duration < 0:
            skipped += 1
            continue
        segments.append({"start": start, "end": min(start + duration, float(MAX_SECONDS)), "text": text})
    if any(b["start"] < a["start"] for a, b in zip(segments, segments[1:], strict=False)):
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
        if not isinstance(language, str) or not LANG_CODE_RE.match(language) or len(language) > 35:
            language = None  # never store something the loader would reject
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


def decode_input(data: bytes, name: str) -> str:
    """Decode imported text as UTF-8 (a leading BOM is allowed). Invalid bytes are an error, never replaced."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        die(f"{name} is not valid UTF-8 text (invalid byte at position {exc.start}). Save or export it as UTF-8 "
            "and try again; nothing was imported.", EXIT_BAD_INPUT)


def _too_large(name: str):
    die(f"{name} is too large (the limit is {MAX_INPUT_BYTES // (1024 * 1024)} MB of UTF-8 text).", EXIT_BAD_INPUT)


def read_stdin_bytes() -> bytes:
    """Read standard input as bytes, enforcing MAX_INPUT_BYTES on the encoded size."""
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:  # the normal case: raw bytes, independent of the console's locale
        data = buffer.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            _too_large("The input")
        return data
    chunks, total = [], 0  # a text-only stream: count the UTF-8 size of what it yields, chunk by chunk
    while True:
        chunk = sys.stdin.read(1024 * 1024)
        if not chunk:
            break
        encoded = chunk.encode("utf-8", errors="surrogateescape")
        total += len(encoded)
        if total > MAX_INPUT_BYTES:
            _too_large("The input")
        chunks.append(encoded)
    return b"".join(chunks)


def read_input_text(file_arg: str) -> str:
    """Read the text to import from a file or stdin, refusing non-files and oversized input."""
    if file_arg == "-":
        return decode_input(read_stdin_bytes(), "The input")
    try:
        path = Path(file_arg)
        if not path.is_file():  # also rejects folders, devices and pipes, which could block or never end
            die(f"Cannot read {path.name or file_arg!r}: it is not an existing regular file.", EXIT_BAD_INPUT)
        with open(path, "rb") as handle:
            data = handle.read(MAX_INPUT_BYTES + 1)
    except (OSError, ValueError) as exc:
        die(f"Cannot read {file_arg!r} ({type(exc).__name__}).", EXIT_BAD_INPUT)
    if len(data) > MAX_INPUT_BYTES:
        _too_large(path.name)
    return decode_input(data, path.name)


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
    try:
        segments, source = parse_transcript_text(text, warnings)
    except ParseError as exc:
        die(str(exc), EXIT_BAD_INPUT)
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
    allowed, reason = cleanup_allowed(store)
    if not allowed and reason == "optin":
        print("Cleanup skipped: TUBEGPT_STORAGE_DIR points at your own folder, and nothing there is deleted unless "
              "you also set TUBEGPT_STORAGE_CLEANUP=1. No files were touched.")
        return 0
    if not allowed:
        print("Cleanup skipped: the folder in TUBEGPT_STORAGE_DIR has no valid tubeGPT marker file "
              f"({STORE_MARKER}), so it is not treated as a tubeGPT store. No files were touched.")
        return 0
    # Hold the same lock as save and read, so a transcript cannot be refreshed between the final re-check and the
    # delete. If the lock is unavailable, cleanup still re-checks every file before removing it.
    with store_lock(store) as locked:
        if not locked:
            log.debug("event=cleanup_without_lock")
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
        if len(line) > a.max_chars:
            line = limit_text(line, a.max_chars)
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


def limit_text(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters and say how much was left out."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]} [... cut; {len(text) - limit} more characters. Raise --max-chars to see them.]"


def iter_hits(segs, terms):
    """Yield, in order, the index of every segment in which any term starts.

    A match may run on into the following segments (a phrase said across two captions), so each segment is
    searched together with just enough of what follows. Nothing is joined into one big string.
    """
    longest = max(len(t) for t in terms)
    n = len(segs)
    window = collections.deque()  # lowered text of segment i and of the segments after it
    after = 0  # characters in window beyond its first entry, separators included
    loaded = 0
    for i in range(n):
        while loaded < n and (loaded <= i or after < longest):
            lowered = segs[loaded]["text"].lower()
            if window:
                after += len(lowered) + 1
            window.append(lowered)
            loaded += 1
        low = window[0]
        tail = low
        if len(window) > 1:
            tail = low + " " + " ".join(itertools.islice(window, 1, None))[:longest]
        for needle in terms:
            if tail.find(needle, 0, len(low) + len(needle) - 1) >= 0:
                yield i
                break
        window.popleft()
        if window:
            after -= len(window[0]) + 1  # the next segment becomes the current one, no longer "after"


def search_windows(hits, n, context):
    """Group hits into non-overlapping windows of ``context`` segments on each side.

    Hits whose windows overlap share one window (up to a bounded size); otherwise the later window is trimmed so
    no segment is ever listed twice. Yields (first line, last line, [hit indexes]).
    """
    cap = 4 * context + 1
    cur = None
    for h in hits:
        lo, hi = max(0, h - context), min(n - 1, h + context)
        if cur is not None:
            if h <= cur[1]:  # already inside the previous window
                cur[2].append(h)
                continue
            if lo <= cur[1] and hi - cur[0] + 1 <= cap:
                cur[1] = hi
                cur[2].append(h)
                continue
            lo = max(lo, cur[1] + 1)
            yield tuple(cur)
        cur = [lo, hi, [h]]
    if cur is not None:
        yield tuple(cur)


def cmd_search(a):
    terms = [re.sub(r"\s+", " ", t.strip().lower()) for t in a.terms]
    terms = [t for t in terms if t]
    if not terms:
        die("Give at least one non-empty search term.", EXIT_BAD_INPUT)
    if len(terms) > MAX_TERMS:
        die(f"Too many search terms ({len(terms)}); the limit is {MAX_TERMS}.", EXIT_BAD_INPUT)
    doc, _ = load_doc(a.src)
    segs = doc["segments"]
    timed = doc.get("has_timestamps") and doc.get("video_id")

    def place(idx):
        where = seg_label(doc, idx)
        return where + " " + link_url(doc["video_id"], segs[idx]["start"]) if timed else where

    reported, used, found = 0, 0, False
    for lo, hi, hit_list in search_windows(iter_hits(segs, terms), len(segs), a.context):
        found = True
        if reported >= a.max:
            print(f"[... more matches exist; showing the first {a.max}. Narrow the terms or raise --max.]")
            return 0
        header = place(hit_list[0])
        if len(hit_list) > 1:
            more = ", ".join(place(h) for h in hit_list[1:4])
            rest = len(hit_list) - 4
            header += f"  (also: {more}{f', and {rest} more' if rest > 0 else ''})"
        body = " ".join(segs[j]["text"] for j in range(lo, hi + 1))
        block = f"{header}\n    {limit_text(body, a.max_chars)}"
        if reported and used + len(block) + 1 > a.max_chars:
            print("[... more matches exist; stopped because of the --max-chars output limit. "
                  "Narrow the terms or raise --max-chars.]")
            return 0
        print(block)
        used += len(block) + 1
        reported += 1
    if not found:
        print("No matches for: " + ", ".join(repr(t) for t in a.terms) +
              "\n(Try other wording or synonyms. Auto-generated captions can also misspell names and terms.)")
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
        if len(text) > 12 or not re.fullmatch(r"[+-]?[0-9]+", text.strip()):
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
    se.add_argument("--max-chars", type=bounded_int(1, 1_000_000), default=30000,
                    help="stop after about this many characters of output (1-1000000, default 30000)")
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
        # Log only the exception type and where it happened: its message could contain input text or paths.
        where = ",".join(f"{Path(f.filename).name}:{f.lineno}" for f in traceback.extract_tb(exc.__traceback__)[-3:])
        log.debug("event=unexpected_error type=%s where=%s", type(exc).__name__, where)
        die(f"Unexpected internal error ({type(exc).__name__}). Re-run with --verbose for details.", EXIT_ERROR)


if __name__ == "__main__":
    sys.exit(main())
