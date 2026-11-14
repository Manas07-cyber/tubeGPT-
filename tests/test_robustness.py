"""Regression and stress tests for the second hardening pass.

Covers: the segment-size limit on every path that creates a document, numeric overflow, byte-accurate input
limits with multibyte text, search windows that never repeat a line, the search matcher against a naive
reference, the cleanup command taking the store lock, and large inputs.
"""
import io
import json
import random
import re
import sys
import time
import types

import pytest

VID = "dQw4w9WgXcQ"


# ------------------------------------------------------------------ segment limit on every generation path
def reload_ok(run, path):
    """The document must be readable by the program's own loader."""
    code, out, err = run("show", str(path), "--max-chars", "1000000")
    assert code == 0, err
    return out


def stored_text(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc, [s["text"] for s in doc["segments"]]


@pytest.fixture
def small_limit(tr, monkeypatch):
    monkeypatch.setattr(tr, "MAX_SEGMENT_CHARS", 50)
    return 50


def test_plain_text_line_longer_than_the_limit_is_split_and_reloads(run, tr, small_limit, tmp_path):
    src = tmp_path / "long.txt"
    src.write_text("word " * 200, encoding="utf-8")
    out = tmp_path / "o.json"
    assert run("parse", str(src), "--out", str(out))[0] == 0
    doc, texts = stored_text(out)
    assert len(texts) > 1 and all(len(t) <= small_limit for t in texts)
    assert " ".join(texts).split() == ("word " * 200).split()      # nothing lost, nothing added
    reload_ok(run, out)


def test_unspaced_cjk_text_is_split_at_the_limit_without_loss(run, tr, small_limit, tmp_path):
    src = tmp_path / "cjk.txt"
    src.write_text("漢" * 170, encoding="utf-8")
    out = tmp_path / "o.json"
    assert run("parse", str(src), "--out", str(out))[0] == 0
    _, texts = stored_text(out)
    assert all(len(t) <= small_limit for t in texts) and "".join(texts) == "漢" * 170
    reload_ok(run, out)


def test_srt_cue_longer_than_the_limit_keeps_its_timestamp_on_every_piece(run, tr, small_limit, tmp_path):
    src = tmp_path / "c.srt"
    src.write_text("1\n00:00:05,000 --> 00:00:09,000\n" + "alpha beta " * 40 + "\n", encoding="utf-8")
    out = tmp_path / "o.json"
    assert run("parse", str(src), "--out", str(out), "--video", VID)[0] == 0
    doc, texts = stored_text(out)
    assert len(texts) > 1 and all(len(t) <= small_limit for t in texts)
    assert {s["start"] for s in doc["segments"]} == {5.0} and doc["has_timestamps"] is True
    assert " ".join(texts).split() == ("alpha beta " * 40).split()
    reload_ok(run, out)


def test_panel_entry_longer_than_the_limit_is_split_and_reloads(run, tr, small_limit, tmp_path):
    src = tmp_path / "p.txt"
    src.write_text("0:01 " + "hello there " * 30 + "\n0:09 the end\n", encoding="utf-8")
    out = tmp_path / "o.json"
    assert run("parse", str(src), "--out", str(out))[0] == 0
    doc, texts = stored_text(out)
    assert all(len(t) <= small_limit for t in texts) and texts[-1] == "the end"
    reload_ok(run, out)


def fake_api(monkeypatch, entries, language="en"):
    class Fetched:
        def to_raw_data(self):
            return entries

    class Transcript:
        language_code, is_generated = language, False

        def fetch(self):
            return Fetched()

    class TList:
        def find_transcript(self, prefs):
            return Transcript()

        def __iter__(self):
            return iter([Transcript()])

    class Api:
        def list(self, vid):
            return TList()

        def fetch(self, vid):  # only here so the "new enough" check passes
            raise AssertionError("not used")

    module = types.ModuleType("youtube_transcript_api")
    module.YouTubeTranscriptApi = Api
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", module)


def test_fetched_segment_longer_than_the_limit_is_split_and_reloads(run, tr, small_limit, tmp_path, monkeypatch):
    fake_api(monkeypatch, [{"text": "long " * 100, "start": 3.0, "duration": 4.0},
                           {"text": "short", "start": 8.0, "duration": 1.0}])
    out = tmp_path / "f.json"
    code, _, err = run("fetch", VID, "--out", str(out))
    assert code == 0, err
    doc, texts = stored_text(out)
    assert all(len(t) <= small_limit for t in texts) and texts[-1] == "short"
    assert [s["start"] for s in doc["segments"]][:2] == [3.0, 3.0]
    reload_ok(run, out)


def test_every_generated_document_passes_the_loader_validation(tr):
    """make_doc output for hostile-but-parseable input is always accepted by validate_doc."""
    rnd = random.Random(7)
    for _ in range(300):
        segs = [{"start": float(i), "end": float(i) + 1.0,
                 "text": "".join(rnd.choice("ab 漢😀‍") for _ in range(rnd.randint(1, 400))).strip() or "x"}
                for i in range(rnd.randint(1, 5))]
        tr.validate_doc(tr.make_doc(segs, source="t"))


def test_save_refuses_a_document_the_loader_would_reject(tr, run, tmp_path):
    bad = {"video_id": VID, "segments": [{"start": 5.0, "text": "b"}, {"start": 1.0, "text": "a"}]}
    with pytest.raises(SystemExit) as exc:
        tr.save_doc(bad, str(tmp_path / "x.json"))
    assert exc.value.code == 2 and not (tmp_path / "x.json").exists()


# ------------------------------------------------------------------ numeric validation
@pytest.mark.parametrize("literal", ["1" + "0" * 400, "-1" + "0" * 400, "1e999", "-1e999", "1e400", "NaN",
                                     "Infinity", "-Infinity", "-0.5", "true", '"5"'])
def test_hostile_numbers_in_a_saved_file_give_a_controlled_error(run, tmp_path, literal):
    f = tmp_path / "n.json"
    for field in ('"start"', '"end"'):
        f.write_text('{"segments":[{"start":0,%s:%s,"text":"x"}]}' % (field, literal), encoding="utf-8")
        code, out, err = run("show", str(f))
        assert code == 2 and "Traceback" not in err + out, (field, literal, err)


def test_duration_overflow_in_a_saved_file_is_controlled(run, tmp_path):
    f = tmp_path / "d.json"
    f.write_text('{"duration_s": 1' + "0" * 400 + ', "segments":[{"start":0,"text":"x"}]}', encoding="utf-8")
    code, _, err = run("show", str(f), "--stats")
    assert code == 2 and "Traceback" not in err


@pytest.mark.parametrize("arg", ["9" * 400, "1e999", "nan", "inf", "-5", "1:99", "1:2:3:4", "", " ", "0x10", "٣"])
def test_hostile_time_arguments_never_crash(run, tr, tmp_path, arg):
    doc = tr.make_doc([{"start": 0.0, "end": 1.0, "text": "x"}], source="t", video_id=VID)
    f = tmp_path / "t.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    for cmd in (("link", str(f), arg), ("show", str(f), "--from", arg), ("show", str(f), "--to", arg)):
        code, _, err = run(*cmd)
        assert code == 2 and "Traceback" not in err, (cmd, err)


def test_hostile_raw_entries_from_the_api_are_skipped_not_fatal(tr):
    raw = [{"text": "ok", "start": 1, "duration": 1},
           {"text": "inf", "start": float("inf"), "duration": 1},
           {"text": "nan", "start": 1, "duration": float("nan")},
           {"text": "neg", "start": -3, "duration": 1},
           {"text": "huge", "start": 10 ** 400, "duration": 1},
           {"text": "long", "start": 1, "duration": 10 ** 400},
           {"text": "string", "start": "x", "duration": 1},
           {"text": None, "start": 2, "duration": 1},
           {"start": 3}, "not a dict"]
    segs, skipped = tr.segments_from_raw(raw)
    assert [s["text"] for s in segs] == ["ok"] and skipped >= 5


# ------------------------------------------------------------------ input limits are measured in bytes
def with_stdin(monkeypatch, payload: bytes, binary=True):
    if binary:
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8"))
    else:
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload.decode("utf-8")))


@pytest.mark.parametrize("binary", [True, False], ids=["byte stream", "text-only stream"])
@pytest.mark.parametrize("text", ["नमस्ते दुनिया " * 6, "😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀", "日本語のテキスト" * 5],
                         ids=["devanagari", "emoji", "japanese"])
def test_stdin_limit_counts_encoded_bytes_not_characters(run, tr, monkeypatch, tmp_path, text, binary):
    data = text.encode("utf-8")
    assert len(text) < len(data)
    limit = len(text) + 5                       # more than the character count, fewer than the byte count
    assert limit < len(data)
    monkeypatch.setattr(tr, "MAX_INPUT_BYTES", limit)
    with_stdin(monkeypatch, data, binary)
    code, _, err = run("parse", "-", "--out", str(tmp_path / "a.json"))
    assert code == 2 and "too large" in err and not (tmp_path / "a.json").exists()
    monkeypatch.setattr(tr, "MAX_INPUT_BYTES", len(data))           # exactly at the limit is accepted
    with_stdin(monkeypatch, data, binary)
    assert run("parse", "-", "--out", str(tmp_path / "b.json"))[0] == 0


def test_file_input_limit_counts_encoded_bytes_too(run, tr, monkeypatch, tmp_path):
    text = "नमस्ते " * 10
    f = tmp_path / "h.txt"
    f.write_text(text, encoding="utf-8")
    monkeypatch.setattr(tr, "MAX_INPUT_BYTES", len(text) + 5)
    assert run("parse", str(f))[0] == 2


def test_invalid_utf8_on_stdin_is_an_error_not_replacement_characters(run, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"first \xff\xfe line\n"), encoding="utf-8"))
    out = tmp_path / "x.json"
    code, _, err = run("parse", "-", "--out", str(out))
    assert code == 2 and "UTF-8" in err and "position" in err and not out.exists()


def test_stdin_with_a_utf8_bom_is_accepted(run, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO("﻿hello\nworld\n".encode("utf-8")),
                                                       encoding="utf-8"))
    out = tmp_path / "x.json"
    assert run("parse", "-", "--out", str(out))[0] == 0
    assert stored_text(out)[1] == ["hello", "world"]


# ------------------------------------------------------------------ pasted transcript names
def test_pasted_transcript_names_are_sha256_and_do_not_collide(tr, isolated_tmp, run, monkeypatch):
    names = set()
    for n in range(40):
        monkeypatch.setattr(sys, "stdin", io.StringIO(f"line one\nline {n}\n"))
        code, out, _ = run("parse", "-")
        assert code == 0
        names.add(re.search(r"saved: .*(pasted-[0-9a-f]+)\.json", out).group(1))
    assert len(names) == 40 and all(len(n) == len("pasted-") + 32 for n in names)


def test_identical_pasted_text_maps_to_one_file(tr):
    doc = tr.make_doc([{"start": None, "text": "same"}], source="t")
    assert tr.pasted_name(doc) == tr.pasted_name(dict(doc))


def test_pasted_name_depends_on_timestamps_and_text(tr):
    a = tr.make_doc([{"start": 1.0, "end": 2.0, "text": "same"}], source="t")
    b = tr.make_doc([{"start": 2.0, "end": 3.0, "text": "same"}], source="t")
    c = tr.make_doc([{"start": 1.0, "end": 2.0, "text": "different"}], source="t")
    assert len({tr.pasted_name(a), tr.pasted_name(b), tr.pasted_name(c)}) == 3


def test_old_eight_character_pasted_files_are_still_readable(tr, isolated_tmp, run):
    store = isolated_tmp
    store.mkdir(mode=0o700)
    old = store / "pasted-0123abcd.json"
    old.write_text(json.dumps({"segments": [{"start": None, "text": "old"}]}), encoding="utf-8")
    assert run("show", str(old))[0] == 0


# ------------------------------------------------------------------ search: matcher and windows
def naive_hits(segs, terms):
    joined = " ".join(s["text"] for s in segs)
    out, offset = [], 0
    for i, seg in enumerate(segs):
        lo, hi = offset, offset + len(seg["text"])
        if any(lo <= m.start() < hi for t in terms for m in re.finditer("(?=" + re.escape(t) + ")", joined)):
            out.append(i)
        offset = hi + 1
    return out


def test_search_matcher_equals_a_naive_reference_on_random_transcripts(tr):
    rnd = random.Random(11)
    words = ["a", "bb", "the cat", "x y z", "ab", "b a", "É", "日本", "a b", "cat the", "नमस्ते"]
    for _ in range(3000):
        segs = [{"start": float(i), "text": " ".join(rnd.choice(words) for _ in range(rnd.randint(1, 3))).lower()}
                for i in range(rnd.randint(1, 9))]
        terms = [" ".join(rnd.choice(words).split()[:rnd.randint(1, 3)]) for _ in range(rnd.randint(1, 3))]
        terms = [t for t in (re.sub(r"\s+", " ", t.strip().lower()) for t in terms) if t]
        if terms:
            assert list(tr.iter_hits(segs, terms)) == naive_hits(segs, terms), (segs, terms)


def test_a_phrase_spanning_two_captions_is_found_once(tr):
    segs = [{"start": 0.0, "text": "we talk about interest"}, {"start": 3.0, "text": "rates and inflation"},
            {"start": 6.0, "text": "nothing here"}]
    assert list(tr.iter_hits(segs, ["interest rates"])) == [0]


@pytest.mark.parametrize("context", [0, 1, 2, 5, 50])
def test_search_windows_never_list_a_line_twice_and_cover_every_hit(tr, context):
    rnd = random.Random(context)
    for _ in range(400):
        n = rnd.randint(1, 60)
        hits = sorted(rnd.sample(range(n), rnd.randint(1, min(n, 25))))
        windows = list(tr.search_windows(iter(hits), n, context))
        seen = set()
        for lo, hi, _hits in windows:
            lines = set(range(lo, hi + 1))
            assert not lines & seen, (hits, windows)
            seen |= lines
            assert 0 <= lo <= hi < n
        assert set(hits) <= seen and [h for w in windows for h in w[2]] == hits


def test_search_output_has_no_repeated_lines_for_adjacent_hits(run, tr, tmp_path):
    segs = [{"start": float(i), "end": float(i) + 1, "text": f"line {i} keyword" if i in (3, 4, 6) else f"line {i}"}
            for i in range(12)]
    f = tmp_path / "s.json"
    f.write_text(json.dumps(tr.make_doc(segs, source="t", video_id=VID)), encoding="utf-8")
    code, out, _ = run("search", str(f), "keyword", "--context", "2")
    assert code == 0
    for i in range(12):
        assert len(re.findall(rf"\bline {i}\b", out)) <= 1, out


def test_search_respects_max_and_max_chars_and_says_so(run, tr, tmp_path):
    segs = [{"start": float(i * 100), "end": float(i * 100) + 1, "text": f"keyword number {i}"} for i in range(40)]
    f = tmp_path / "s.json"
    f.write_text(json.dumps(tr.make_doc(segs, source="t", video_id=VID)), encoding="utf-8")
    code, out, _ = run("search", str(f), "keyword", "--context", "0", "--max", "3")
    assert code == 0 and out.count("youtu.be") == 3 and "more matches exist" in out
    code, out, _ = run("search", str(f), "keyword", "--context", "0", "--max-chars", "300")
    assert code == 0 and "output limit" in out and len(out) < 600


# ------------------------------------------------------------------ cleanup command locking
def test_the_cleanup_command_holds_the_store_lock(run, tr, isolated_tmp, monkeypatch):
    calls = []
    real = tr.store_lock

    def spy(store, **kwargs):
        calls.append(kwargs)
        return real(store, **kwargs)

    monkeypatch.setattr(tr, "store_lock", spy)
    isolated_tmp.mkdir(mode=0o700)
    assert run("cleanup")[0] == 0
    assert len(calls) == 1 and not calls[0].get("shared")


# ------------------------------------------------------------------ stress
def big_doc(tr, n):
    return {"segments": [{"start": float(i), "end": float(i) + 1.0,
                          "text": f"segment number {i} " + ("needle " if i % 50_000 == 7 else "filler words here")}
                         for i in range(n)], "has_timestamps": True, "video_id": VID}


def test_validation_and_search_scale_linearly_on_a_large_transcript(tr):
    doc = big_doc(tr, 300_000)
    start = time.monotonic()
    tr.validate_doc(doc)
    hits = list(tr.iter_hits(doc["segments"], ["needle"]))
    windows = list(tr.search_windows(iter(hits), len(doc["segments"]), 2))
    elapsed = time.monotonic() - start
    assert hits == [7 + 50_000 * k for k in range(6)] and len(windows) == 6
    assert elapsed < 30, f"took {elapsed:.1f}s"             # typically about a second; the bound only catches O(n^2)


def test_cli_search_and_show_on_a_large_saved_transcript(run, tr, tmp_path):
    doc = big_doc(tr, 150_000)
    f = tmp_path / "big.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    start = time.monotonic()
    code, out, _ = run("search", str(f), "needle", "--max", "2")
    assert code == 0 and out.count("youtu.be") == 2
    code, out, _ = run("show", str(f), "--max-chars", "2000")
    assert code == 0 and "stopped after" in out and len(out) < 3000
    assert time.monotonic() - start < 60


def test_search_with_the_maximum_number_of_long_terms_stays_bounded(run, tr, tmp_path):
    doc = big_doc(tr, 20_000)
    f = tmp_path / "t.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    terms = ["t" * 498 + f"{i:02d}" for i in range(tr.MAX_TERMS)]      # 500 characters each
    start = time.monotonic()
    code, out, _ = run("search", str(f), *terms)
    assert code == 0 and "No matches" in out and time.monotonic() - start < 60


def test_a_very_long_single_line_is_parsed_in_reasonable_time(tr):
    start = time.monotonic()
    segs, _ = tr.parse_transcript_text("word " * 400_000)          # about 2 million characters, no line breaks
    doc = tr.make_doc(segs, source="t")                            # oversized text is split, not rejected
    tr.validate_doc(doc)
    assert time.monotonic() - start < 20
    assert all(len(seg["text"]) <= tr.MAX_SEGMENT_CHARS for seg in doc["segments"]) and len(doc["segments"]) >= 20
    assert " ".join(seg["text"] for seg in doc["segments"]).split() == ["word"] * 400_000


def test_a_large_vtt_with_many_cues_parses_quickly(tr):
    def stamp(i, ms):
        return f"{i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}.{ms}"

    cues = "".join(f"{i}\n{stamp(i, '000')} --> {stamp(i, '900')}\nspoken words number {i}\n\n"
                   for i in range(60_000))
    start = time.monotonic()
    segs, source = tr.parse_transcript_text("WEBVTT\n\n" + cues)
    assert source.startswith("caption file") and len(segs) == 60_000 and time.monotonic() - start < 30
    assert segs[-1]["text"] == "spoken words number 59999" and segs[-1]["start"] == 59999.0
