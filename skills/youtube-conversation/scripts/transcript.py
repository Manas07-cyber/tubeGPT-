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

SRC is a saved transcript JSON, or the YouTube URL / video ID it was saved for.
T accepts seconds (754), M:SS (12:34) or H:MM:SS (1:02:03).

Exit codes: 0 ok | 1 unexpected error | 2 bad input | 3 cannot reach YouTube |
            4 no readable transcript for this video | 5 YouTube refused the
            request (blocked / rate limited) | 6 missing or outdated dependency
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import html
import json
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

EXIT_ERROR, EXIT_BAD_INPUT, EXIT_NETWORK = 1, 2, 3
EXIT_NO_TRANSCRIPT, EXIT_BLOCKED, EXIT_MISSING_DEP = 4, 5, 6

ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
TS = r"(?:\d{1,2}:)?\d{1,2}:\d{2}"
TS_MS = TS + r"(?:[.,]\d{1,3})?"
CUE_RE = re.compile(r"^\s*(" + TS_MS + r")\s*-->\s*(" + TS_MS + r")")
PANEL_RE = re.compile(r"^\s*(" + TS + r")(?!\d)\s*[-\u2013\u2014\u00b7\u2022|:]?\s*(.*)$")


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
    return to_seconds(text)


def fmt_time(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def link_url(video_id, seconds: float) -> str:
    return f"https://youtu.be/{video_id}?t={int(seconds)}"


def clean_text(text: str, cue: bool = False) -> str:
    if cue:  # caption files carry <i>, <c>, <00:00:01.500> tags and HTML entities
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)
    text = text.replace("\u200b", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def default_dir() -> Path:
    return Path(tempfile.gettempdir()) / "youtube-conversation"


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


def save_doc(doc, out):
    if out:
        path = Path(out)
    else:
        name = doc.get("video_id")
        if not name:
            digest = hashlib.sha1("".join(s["text"] for s in doc["segments"]).encode("utf-8")).hexdigest()[:8]
            name = "pasted-" + digest
        path = default_dir() / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_doc(src: str):
    path = None
    try:
        if Path(src).is_file():
            path = Path(src)
    except OSError:
        pass
    if path is None:
        vid = extract_video_id(src)
        if vid and (default_dir() / f"{vid}.json").is_file():
            path = default_dir() / f"{vid}.json"
    if path is None:
        die(f"No saved transcript for {src!r}. Run 'fetch' (or 'parse' for pasted text) first.", EXIT_BAD_INPUT)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or "segments" not in doc:
            raise ValueError("missing segments")
    except (ValueError, OSError):
        die(f"{path} is not a saved transcript. Use 'parse' to import a pasted transcript or caption file.", EXIT_BAD_INPUT)
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
def parse_cues(text: str):
    """SRT and WebVTT. Drops the repeated lines YouTube's rolling auto-captions produce."""
    lines = text.splitlines()
    segments, prev_last, i = [], None, 0
    while i < len(lines):
        m = CUE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        start, end = to_seconds(m.group(1)), to_seconds(m.group(2))
        i += 1
        block = []
        while i < len(lines) and lines[i].strip() and not CUE_RE.match(lines[i]):
            block.append(clean_text(lines[i], cue=True))
            i += 1
        block = [b for b in block if b]
        if not block:
            continue
        fresh = block[1:] if prev_last is not None and block[0] == prev_last else block
        prev_last = block[-1]
        if fresh:
            segments.append({"start": start, "end": end, "text": " ".join(fresh)})
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
        if m:
            t = to_seconds(m.group(1))
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
    lines = [clean_text(l) for l in text.splitlines()]
    lines = [l for l in lines if l]
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
    return [{"start": None, "text": l} for l in lines]


def parse_transcript_text(text: str):
    text = text.lstrip("\ufeff")
    lines = text.splitlines()
    if any(CUE_RE.match(l) for l in lines):
        segs = parse_cues(text)
        if segs:
            return segs, "caption file (srt/vtt)"
    if sum(1 for l in lines if PANEL_RE.match(l)) >= 2:
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
    except Exception:
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
    prefs = [l for l in langs if l.lower() != "any"]
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
    """Best-effort title/channel lookup. Never fails the command."""
    import urllib.request
    target = "https://www.youtube.com/oembed?format=json&url=" + quote(
        f"https://www.youtube.com/watch?v={video_id}", safe="")
    try:
        with urllib.request.urlopen(target, timeout=6) as resp:
            data = json.load(resp)
        return data.get("title"), data.get("author_name")
    except Exception:
        return None, None


def requirements_hint() -> str:
    """Path of the plugin's requirements.txt (two folders above this skill), if it can be found."""
    candidate = Path(__file__).resolve().parents[3] / "requirements.txt"
    return f'"{candidate}"' if candidate.is_file() else "requirements.txt"


def cmd_fetch(a):
    vid = extract_video_id(a.video)
    if not vid:
        die(f"Could not find a YouTube video ID in {a.video!r}.", EXIT_BAD_INPUT)
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        die("The 'youtube-transcript-api' package is not installed. Create and activate a virtual environment, "
            f"then run: python -m pip install -r {requirements_hint()} (see README.md for the exact steps).", EXIT_MISSING_DEP)
    if not (hasattr(YouTubeTranscriptApi, "fetch") and hasattr(YouTubeTranscriptApi, "list")):
        die("The installed youtube-transcript-api is too old (version 1.0 or newer is needed). "
            f"Inside your virtual environment, run: python -m pip install -U -r {requirements_hint()}", EXIT_MISSING_DEP)

    langs = [l.strip() for l in a.lang.split(",") if l.strip()] or ["en", "any"]
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

    segments = []
    for item in raw:
        text = clean_text(item.get("text", ""))
        if text:
            start = float(item["start"])
            segments.append({"start": start, "end": start + float(item.get("duration") or 0), "text": text})
    if not segments:
        die("YouTube returned an empty transcript for this video.", EXIT_NO_TRANSCRIPT)

    title, channel = try_oembed(vid)
    doc = make_doc(segments, source="youtube-transcript-api", video_id=vid, language=language,
                   is_generated=generated, title=title, channel=channel)
    print(describe(doc, save_doc(doc, a.out)))
    return 0


def cmd_parse(a):
    if a.file == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(a.file).read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            die(f"Cannot read {a.file}: {exc}", EXIT_BAD_INPUT)
    if not text.strip():
        die("The transcript text is empty.", EXIT_BAD_INPUT)
    vid = None
    if a.video:
        vid = extract_video_id(a.video)
        if not vid:
            die(f"Could not find a YouTube video ID in {a.video!r}.", EXIT_BAD_INPUT)
    segments, source = parse_transcript_text(text)
    if not segments:
        die("Could not find any transcript text in that input.", EXIT_BAD_INPUT)
    doc = make_doc(segments, source=source, video_id=vid, title=a.title)
    print(describe(doc, save_doc(doc, a.out)))
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
    doc, _ = load_doc(a.src)
    segs = doc["segments"]
    lowered = [s["text"].lower() for s in segs]
    offsets, pos = [], 0
    for t in lowered:
        offsets.append(pos)
        pos += len(t) + 1
    full = " ".join(lowered)

    hits = set()
    for term in a.terms:
        needle = re.sub(r"\s+", " ", term.strip().lower())
        if not needle:
            continue
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
    for value in a.times:
        seconds = int(parse_time_arg(value))
        label = fmt_time(seconds)
        if doc.get("video_id"):
            print(f"{value} => [{label}]({link_url(doc['video_id'], seconds)})")
        else:
            print(f"{value} => [{label}]  (no video URL known, so no link)")
    return 0


# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(description="Timestamped YouTube transcripts for the youtube-conversation skill.")
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="download a video's transcript")
    f.add_argument("video", help="YouTube URL or 11-character video ID")
    f.add_argument("--lang", default="en,any",
                   help="preferred language codes in priority order; 'any' = fall back to whatever exists (default: en,any)")
    f.add_argument("--out", help="where to save the JSON (default: temp folder, named by video ID)")
    f.set_defaults(func=cmd_fetch)

    pa = sub.add_parser("parse", help="import a pasted transcript or .srt/.vtt/.txt file")
    pa.add_argument("file", help="path to the text file, or - for stdin")
    pa.add_argument("--video", help="YouTube URL or ID, so timestamps can become links")
    pa.add_argument("--title", help="optional title label")
    pa.add_argument("--out", help="where to save the JSON")
    pa.set_defaults(func=cmd_parse)

    s = sub.add_parser("show", help="print the transcript with timestamps")
    s.add_argument("src")
    s.add_argument("--stats", action="store_true", help="print details only, not the text")
    s.add_argument("--from", dest="t_from", help="start time, inclusive (754, 12:34, 1:02:03)")
    s.add_argument("--to", dest="t_to", help="end time, exclusive")
    s.add_argument("--max-chars", type=int, default=30000, help="stop after about this many characters (default 30000)")
    s.set_defaults(func=cmd_show)

    se = sub.add_parser("search", help="find where terms are said")
    se.add_argument("src")
    se.add_argument("terms", nargs="+", help="terms or quoted phrases; a match on any of them counts")
    se.add_argument("--context", type=int, default=2, help="neighbouring lines to show on each side (default 2)")
    se.add_argument("--max", type=int, default=15, help="maximum places to show (default 15)")
    se.set_defaults(func=cmd_search)

    li = sub.add_parser("link", help="make markdown timestamp links")
    li.add_argument("src")
    li.add_argument("times", nargs="+")
    li.set_defaults(func=cmd_link)
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    if getattr(args, "context", 0) is not None and getattr(args, "context", 0) < 0:
        die("--context cannot be negative.", EXIT_BAD_INPUT)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
