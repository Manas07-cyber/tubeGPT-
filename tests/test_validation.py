"""Saved-transcript validation: every malformed structure must give a controlled error, never a crash."""
import json
import time

import pytest

VID = "dQw4w9WgXcQ"


def good(**overrides):
    doc = {"video_id": VID, "url": f"https://www.youtube.com/watch?v={VID}", "title": "T", "channel": "C",
           "language": "en", "is_generated": False, "source": "youtube-transcript-api", "has_timestamps": True,
           "duration_s": 4.0, "segments": [{"start": 0.0, "end": 2.0, "text": "one"},
                                           {"start": 2.0, "end": 4.0, "text": "two"}]}
    doc.update(overrides)
    return doc


def seg(**kw):
    base = {"start": 1.0, "end": 2.0, "text": "x"}
    base.update(kw)
    return base


INVALID = {
    "root is a list": [good()],
    "root is a string": "hello",
    "root is null": None,
    "root is a number": 5,
    "segments missing": {k: v for k, v in good().items() if k != "segments"},
    "segments is a string": good(segments="abc"),
    "segments is an object": good(segments={"a": 1}),
    "segments empty": good(segments=[]),
    "segment is a string": good(segments=["abc"]),
    "segment is null": good(segments=[None]),
    "segment is a list": good(segments=[["a"]]),
    "text missing": good(segments=[{"start": 1.0}]),
    "text is a number": good(segments=[seg(text=5)]),
    "text is null": good(segments=[seg(text=None)]),
    "text is a list": good(segments=[seg(text=["a"])]),
    "text enormous": good(segments=[seg(text="x" * 100_001)]),
    "start is a string": good(segments=[seg(start="1")]),
    "start is a bool": good(segments=[seg(start=True)]),
    "start is a list": good(segments=[seg(start=[1])]),
    "start negative": good(segments=[seg(start=-1)]),
    "start absurdly large": good(segments=[seg(start=1e12)]),
    "end is a string": good(segments=[seg(end="2")]),
    "end negative": good(segments=[seg(start=0, end=-5)]),
    "end before start": good(segments=[seg(start=5.0, end=4.0)]),
    "end without start": good(segments=[{"start": None, "end": 3.0, "text": "x"}], has_timestamps=False),
    "some segments untimed": good(segments=[seg(), {"start": None, "text": "y"}]),
    "starts out of order": good(segments=[seg(start=5.0, end=6.0), seg(start=1.0, end=2.0)]),
    "has_timestamps is a string": good(has_timestamps="yes"),
    "has_timestamps true but untimed": good(has_timestamps=True, segments=[{"start": None, "text": "x"}]),
    "has_timestamps false but timed": good(has_timestamps=False),
    "video_id invalid": good(video_id="not an id"),
    "video_id has path characters": good(video_id="../../evil"),
    "video_id is a number": good(video_id=12345678901),
    "url is a number": good(url=5),
    "title is a list": good(title=["a"]),
    "title enormous": good(title="t" * 1001),
    "language enormous": good(language="e" * 36),
    "source is a dict": good(source={"a": 1}),
    "is_generated is a string": good(is_generated="no"),
    "duration is a string": good(duration_s="4"),
    "duration negative": good(duration_s=-1),
    "duration absurd": good(duration_s=1e20),
}


@pytest.fixture
def write_doc(tmp_path):
    def _write(content, name="doc.json"):
        path = tmp_path / name
        if isinstance(content, (bytes, bytearray)):
            path.write_bytes(content)
        elif isinstance(content, str) and name.endswith(".raw"):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_text(json.dumps(content), encoding="utf-8")
        return path
    return _write


@pytest.mark.parametrize("label", list(INVALID))
@pytest.mark.parametrize("command", [("show",), ("show", "--stats"), ("search", "--q"), ("link", "--t")])
def test_invalid_documents_are_rejected_cleanly_by_every_command(run, write_doc, label, command):
    path = write_doc(INVALID[label])
    args = list(command)
    if args[0] == "show":
        argv = args[:1] + [str(path)] + args[1:]
    elif args[0] == "search":
        argv = ["search", str(path), "x"]
    else:
        argv = ["link", str(path), "5"]
    code, out, err = run(*argv)
    assert code == 2, (label, err)
    assert "not a saved transcript" in err and "Traceback" not in err and out == ""


@pytest.mark.parametrize("raw,fragment", [
    ('{"segments": [{"start": NaN, "text": "x"}]}', "invalid number NaN"),
    ('{"segments": [{"start": Infinity, "text": "x"}]}', "invalid number Infinity"),
    ('{"segments": [{"start": 1, "end": -Infinity, "text": "x"}]}', "invalid number -Infinity"),
    ('{"segments": [{"start": 1, "text": "x"}], "duration_s": NaN}', "invalid number NaN"),
    ("{not json", "not valid JSON"),
    ("", "not valid JSON"),
    ('{"segments": [', "not valid JSON"),
])
def test_invalid_json_text_is_reported(run, write_doc, raw, fragment):
    path = write_doc(raw, "doc.raw")
    code, _, err = run("show", str(path))
    assert code == 2 and fragment in err and "Traceback" not in err


def test_invalid_utf8_file_is_reported(run, write_doc):
    path = write_doc(b'{"segments": [{"text": "caf\xe9"}]}', "bad.raw")
    code, _, err = run("show", str(path))
    assert code == 2 and "not valid UTF-8" in err


def test_deeply_nested_json_does_not_crash(run, write_doc):
    path = write_doc("[" * 200_000 + "]" * 200_000, "deep.raw")
    code, _, err = run("show", str(path))
    assert code == 2 and "Traceback" not in err


def test_binary_garbage_is_reported(run, write_doc):
    path = write_doc(bytes(range(256)) * 20, "junk.raw")
    code, _, err = run("search", str(path), "x")
    assert code == 2 and "not a saved transcript" in err


def test_oversized_file_is_refused_without_reading_it(run, tr, write_doc, monkeypatch):
    monkeypatch.setattr(tr, "MAX_DOC_BYTES", 100)
    path = write_doc(good())
    code, _, err = run("show", str(path))
    assert code == 2 and "too large" in err


def test_too_many_segments_are_refused(tr, monkeypatch):
    monkeypatch.setattr(tr, "MAX_SEGMENTS", 3)
    doc = good(segments=[seg(start=float(i), end=float(i)) for i in range(4)])
    with pytest.raises(tr.InvalidDocument, match="more than 3 segments"):
        tr.validate_doc(doc)


def test_error_messages_do_not_expose_directories(run, write_doc, tmp_path):
    path = write_doc({"nope": 1})
    code, _, err = run("show", str(path))
    assert code == 2 and str(tmp_path) not in err and path.name in err


# ---------------------------------------------------------------- what must keep working
def test_current_format_is_accepted(tr):
    assert tr.validate_doc(good())["segments"][1]["text"] == "two"


def test_minimal_document_and_missing_optional_fields_are_accepted(run, write_doc):
    path = write_doc({"segments": [{"text": "hello there"}]})
    code, out, _ = run("show", str(path))
    assert code == 0 and "#0 hello there" in out


def test_has_timestamps_is_derived_when_absent(tr):
    doc = good()
    del doc["has_timestamps"]
    assert tr.validate_doc(doc)["has_timestamps"] is True


def test_unknown_extra_keys_are_ignored_for_forward_compatibility(tr):
    doc = good(future_field={"a": 1}, segments=[seg(speaker="A")])
    assert tr.validate_doc(doc)


def test_integers_and_equal_starts_are_fine(tr):
    doc = good(segments=[{"start": 1, "end": 1, "text": "a"}, {"start": 1, "text": "b"}, {"start": 2, "text": "c"}])
    assert tr.validate_doc(doc)


def test_documents_with_utf8_bom_are_accepted(run, write_doc):
    path = write_doc(b"\xef\xbb\xbf" + json.dumps(good()).encode("utf-8"), "bom.raw")
    assert run("show", str(path))[0] == 0


def test_unicode_text_round_trips(run, tmp_path):
    f = tmp_path / "u.txt"
    f.write_text("ನಮಸ್ಕಾರ ಪ್ರಪಂಚ\nनमस्ते दुनिया\n😀 ok", encoding="utf-8")
    out = tmp_path / "u.json"
    assert run("parse", str(f), "--out", str(out))[0] == 0
    code, shown, _ = run("show", str(out))
    assert code == 0 and "ನಮಸ್ಕಾರ" in shown and "😀" in shown


# ---------------------------------------------------------------- empty and very large transcripts
def test_empty_segments_in_a_saved_file_are_rejected(run, write_doc):
    code, _, err = run("show", str(write_doc(good(segments=[]))))
    assert code == 2 and "no transcript segments" in err


def test_empty_input_to_parse_is_rejected(run, tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("", encoding="utf-8")
    code, _, err = run("parse", str(f))
    assert code == 2 and "empty" in err


def test_very_large_transcript_works_end_to_end(run, tr, tmp_path):
    n = 120_000
    segments = [{"start": float(i), "end": float(i) + 0.9, "text": f"line {i} " + ("needle " if i == 99_999 else "pad")}
                for i in range(n)]
    path = tmp_path / "big.json"
    path.write_text(json.dumps(tr.make_doc(segments, source="test", video_id=VID)), encoding="utf-8")
    started = time.monotonic()
    code, out, _ = run("search", str(path), "needle", "--context", "0")
    assert code == 0 and "line 99999" in out and "?t=99999" in out
    code, out, _ = run("show", str(path), "--from", "50000", "--to", "50005")
    assert code == 0 and out.count("\n") == 5
    code, out, _ = run("show", str(path), "--stats")
    assert code == 0 and f"segments: {n}" in out
    assert time.monotonic() - started < 30                           # generous: only catches quadratic behaviour
