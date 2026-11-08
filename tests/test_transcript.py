import json
import sys
import types

import pytest

VID = "dQw4w9WgXcQ"


# ---------------------------------------------------------------- URL handling
@pytest.mark.parametrize("value", [
    VID,
    f"https://www.youtube.com/watch?v={VID}",
    f"https://youtube.com/watch?v={VID}&t=30s",
    f"https://m.youtube.com/watch?v={VID}",
    f"www.youtube.com/watch?v={VID}",
    f"https://youtu.be/{VID}?si=abc",
    f"https://www.youtube.com/shorts/{VID}",
    f"https://www.youtube.com/embed/{VID}",
    f"https://www.youtube.com/live/{VID}",
    f"https://www.youtube-nocookie.com/embed/{VID}",
    f"  {VID}  ",
])
def test_extract_video_id_valid(tr, value):
    assert tr.extract_video_id(value) == VID


@pytest.mark.parametrize("value", [
    "", None, "   ", "not a url", "short", "https://example.com/watch?v=" + VID,
    "https://evilyoutube.com/watch?v=" + VID,
    "https://youtube.com.evil.com/watch?v=" + VID,
    "https://www.youtube.com/watch?v=tooshort",
    "https://www.youtube.com/watch",
    "https://www.youtube.com/playlist?list=PL123",
    "https://youtu.be/",
    "https://www.youtube.com/watch?v=" + VID + "X",
    "http://[::1",  # malformed URL must not raise
])
def test_extract_video_id_invalid(tr, value):
    assert tr.extract_video_id(value) is None


# ---------------------------------------------------------------- time helpers
@pytest.mark.parametrize("text,expected", [
    ("754", 754), ("12:34", 754), ("1:02:03", 3723), ("0:00", 0), ("00:00:01,500", 1.5),
])
def test_to_seconds(tr, text, expected):
    assert tr.to_seconds(text) == expected


@pytest.mark.parametrize("seconds,expected", [
    (0, "0:00"), (9, "0:09"), (754, "12:34"), (3599, "59:59"), (3600, "1:00:00"), (3723.9, "1:02:03"),
])
def test_fmt_time(tr, seconds, expected):
    assert tr.fmt_time(seconds) == expected


def test_link_url_truncates_to_whole_seconds(tr):
    assert tr.link_url(VID, 754.9) == f"https://youtu.be/{VID}?t=754"


@pytest.mark.parametrize("bad", ["abc", "", "12:ab", "-5", "-1:30", "1:-30", "nan", "inf", "-inf", "1:nan"])
def test_parse_time_arg_rejects_invalid_or_negative(tr, bad):
    with pytest.raises(SystemExit) as exc:
        tr.parse_time_arg(bad)
    assert exc.value.code == tr.EXIT_BAD_INPUT


def test_parse_time_arg_accepts_valid(tr):
    assert tr.parse_time_arg("1:02:03") == 3723


# ---------------------------------------------------------------- parsing
def test_parse_srt(tr, docs):
    segs, source = tr.parse_transcript_text(docs.SRT)
    assert "srt/vtt" in source
    assert [s["start"] for s in segs] == [1.0, 3.5, 65.25]
    assert segs[0]["text"] == "Hello and welcome to the show."      # <i> tags stripped
    assert segs[1]["text"] == "Today we talk about interest rates & inflation."  # entity decoded
    assert segs[2]["end"] == 69.0


def test_parse_vtt_drops_header_and_rolling_duplicates(tr, docs):
    segs, _ = tr.parse_transcript_text(docs.VTT)
    assert [s["text"] for s in segs] == ["Welcome back.", "This line is new.", "An hour in, we discuss caching."]
    assert segs[-1]["start"] == 3723.0


def test_parse_panel_text(tr, docs):
    segs, source = tr.parse_transcript_text(docs.PANEL)
    assert "with timestamps" in source
    assert [(s["start"], s["text"]) for s in segs] == [
        (0.0, "Intro to the topic"), (15.0, "second thought spans two lines"), (3723.0, "Late segment")]


def test_parse_plain_text_has_no_timestamps(tr, docs):
    segs, source = tr.parse_transcript_text(docs.PLAIN)
    assert "no timestamps" in source
    assert len(segs) == 3 and all(s["start"] is None for s in segs)


def test_parse_plain_long_single_line_is_chunked(tr):
    text = " ".join(f"Sentence number {i} is here." for i in range(200))
    segs, _ = tr.parse_transcript_text(text)
    assert len(segs) > 1 and all(len(s["text"]) <= 900 for s in segs)


def test_parse_handles_bom(tr, docs):
    segs, _ = tr.parse_transcript_text("﻿" + docs.SRT)
    assert len(segs) == 3


# ---------------------------------------------------------------- CLI: parse/show/search/link
def parse_file(run, tmp_path, text, name="t.txt", video=True):
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    args = ["parse", str(f), "--out", str(tmp_path / "doc.json")]
    if video:
        args += ["--video", f"https://youtu.be/{VID}"]
    code, out, err = run(*args)
    return code, out, err, tmp_path / "doc.json"


def test_parse_command_saves_json(run, tmp_path, docs):
    code, out, _, path = parse_file(run, tmp_path, docs.SRT, "a.srt")
    assert code == 0 and path.is_file()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["video_id"] == VID and doc["has_timestamps"] is True and len(doc["segments"]) == 3
    assert "timestamps: yes" in out


def test_parse_command_default_location_uses_store_and_creates_dir(run, tr, tmp_path, docs, isolated_tmp):
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    code, _, _ = run("parse", str(f), "--video", VID)
    assert code == 0
    assert (isolated_tmp / f"{VID}.json").is_file()


def test_pasted_text_without_video_gets_hash_name(run, tmp_path, docs, isolated_tmp):
    f = tmp_path / "n.txt"
    f.write_text(docs.PLAIN, encoding="utf-8")
    assert run("parse", str(f))[0] == 0
    names = [p.name for p in isolated_tmp.iterdir()]
    assert len(names) == 1 and names[0].startswith("pasted-") and names[0].endswith(".json")


def test_parse_empty_input(run, tmp_path):
    code, _, err, _ = parse_file(run, tmp_path, "   \n \n")
    assert code == 2 and "empty" in err


def test_parse_missing_file(run, tmp_path):
    code, _, err = run("parse", str(tmp_path / "nope.srt"))
    assert code == 2 and "Cannot read" in err


def test_parse_bad_video_argument(run, tmp_path, docs):
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    code, _, err = run("parse", str(f), "--video", "https://example.com/x")
    assert code == 2 and "video ID" in err


def test_parse_non_utf8_bytes_do_not_crash(run, tmp_path):
    f = tmp_path / "bin.txt"
    f.write_bytes(b"caf\xe9 line one\nsecond line\n")
    code, _, _ = run("parse", str(f), "--out", str(tmp_path / "o.json"))
    assert code == 0


def test_show_prints_timestamped_lines(run, tmp_path, docs):
    *_, path = parse_file(run, tmp_path, docs.SRT, "a.srt")
    code, out, _ = run("show", str(path))
    assert code == 0
    assert "[0:01] Hello and welcome to the show." in out and "[1:05] Rates went up again." in out


def test_show_range_from_to(run, tmp_path, docs):
    *_, path = parse_file(run, tmp_path, docs.SRT, "a.srt")
    code, out, _ = run("show", str(path), "--from", "0:03", "--to", "1:00")
    assert "interest rates" in out and "Hello" not in out and "Rates went up" not in out
    code, out, _ = run("show", str(path), "--from", "10:00")
    assert "no transcript lines" in out


@pytest.mark.parametrize("flag", ["--from", "--to"])
@pytest.mark.parametrize("bad", ["-5", "1:-30", "nan", "inf", "abc"])
def test_show_rejects_invalid_times(run, tmp_path, docs, flag, bad):
    *_, path = parse_file(run, tmp_path, docs.SRT, "a.srt")
    code, _, err = run("show", str(path), flag, bad)
    assert code == 2 and "ERROR" in err


def test_show_truncates_and_gives_continue_hint(run, tmp_path):
    text = "\n".join(f"00:00:{i:02d},000 --> 00:00:{i:02d},900\nline number {i}\n" for i in range(1, 30))
    *_, path = parse_file(run, tmp_path, text, "long.srt")
    code, out, _ = run("show", str(path), "--max-chars", "80")
    assert code == 0 and "stopped after" in out and "--from" in out


def test_show_stats(run, tmp_path, docs):
    *_, path = parse_file(run, tmp_path, docs.SRT, "a.srt")
    code, out, _ = run("show", str(path), "--stats")
    assert code == 0 and f"video_id: {VID}" in out and "segments: 3" in out


def test_show_untimestamped_uses_line_numbers(run, tmp_path, docs):
    *_, path = parse_file(run, tmp_path, docs.PLAIN)
    code, out, _ = run("show", str(path))
    assert "#0 First line of notes." in out


def test_search_finds_with_link_and_context(run, tmp_path, docs):
    *_, path = parse_file(run, tmp_path, docs.SRT, "a.srt")
    code, out, _ = run("search", str(path), "inflation", "--context", "0")
    assert code == 0
    assert f"[0:03] https://youtu.be/{VID}?t=3" in out and "inflation" in out


def test_search_is_case_insensitive_and_multi_term(run, tmp_path, docs):
    *_, path = parse_file(run, tmp_path, docs.SRT, "a.srt")
    code, out, _ = run("search", str(path), "WELCOME", "went up", "--context", "0")
    assert "[0:01]" in out and "[1:05]" in out


def test_search_phrase_spanning_segments(run, tmp_path, docs):
    *_, path = parse_file(run, tmp_path, docs.SRT, "a.srt")
    code, out, _ = run("search", str(path), "show. today", "--context", "0")
    assert "No matches" not in out


def test_search_no_match(run, tmp_path, docs):
    *_, path = parse_file(run, tmp_path, docs.SRT, "a.srt")
    code, out, _ = run("search", str(path), "zebra")
    assert code == 0 and "No matches" in out


def test_search_negative_context_rejected(run, tmp_path, docs):
    *_, path = parse_file(run, tmp_path, docs.SRT, "a.srt")
    code, _, err = run("search", str(path), "rates", "--context", "-1")
    assert code == 2 and "negative" in err


def test_search_respects_max(run, tmp_path):
    text = "\n".join(f"00:00:{i:02d},000 --> 00:00:{i:02d},900\nneedle number {i}\n" for i in range(1, 20))
    *_, path = parse_file(run, tmp_path, text, "m.srt")
    code, out, _ = run("search", str(path), "needle", "--context", "0", "--max", "3")
    assert out.count("needle number") == 3 and "more matches exist" in out


def test_search_untimestamped_has_no_link(run, tmp_path, docs):
    *_, path = parse_file(run, tmp_path, docs.PLAIN)
    code, out, _ = run("search", str(path), "python", "--context", "0")
    assert "#2" in out and "youtu.be" not in out


def test_link_generates_markdown(run, tmp_path, docs):
    *_, path = parse_file(run, tmp_path, docs.SRT, "a.srt")
    code, out, _ = run("link", str(path), "754", "12:34", "1:02:03")
    assert code == 0
    assert f"[12:34](https://youtu.be/{VID}?t=754)" in out
    assert f"[1:02:03](https://youtu.be/{VID}?t=3723)" in out


@pytest.mark.parametrize("bad", ["-10", "nan", "inf", "xx", "1:-5"])
def test_link_rejects_bad_times(run, tmp_path, docs, bad):
    *_, path = parse_file(run, tmp_path, docs.SRT, "a.srt")
    code, _, err = run("link", str(path), "5", bad)
    assert code == 2 and "ERROR" in err


def test_link_without_timestamps(run, tmp_path, docs):
    *_, path = parse_file(run, tmp_path, docs.PLAIN)
    code, _, err = run("link", str(path), "5")
    assert code == 2 and "no timestamps" in err


def test_link_without_video_url(run, tmp_path, docs):
    *_, path = parse_file(run, tmp_path, docs.SRT, "a.srt", video=False)
    code, out, _ = run("link", str(path), "65")
    assert "no video URL known" in out and "youtu.be" not in out


# ---------------------------------------------------------------- loading / invalid input
def test_load_missing_transcript(run, tmp_path):
    code, _, err = run("show", VID)  # nothing saved for this ID
    assert code == 2 and "No saved transcript" in err


def test_load_non_transcript_json(run, tmp_path):
    f = tmp_path / "x.json"
    f.write_text('{"hello": 1}', encoding="utf-8")
    code, _, err = run("show", str(f))
    assert code == 2 and "not a saved transcript" in err


def test_load_corrupt_json(run, tmp_path):
    f = tmp_path / "x.json"
    f.write_text("{not json", encoding="utf-8")
    code, _, _ = run("show", str(f))
    assert code == 2


def test_load_by_video_id_from_store(run, tmp_path, docs):
    parse_file(run, tmp_path, docs.SRT, "a.srt")  # saved at tmp_path/doc.json
    f = tmp_path / "b.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    run("parse", str(f), "--video", VID)  # saved into the isolated store as <id>.json
    code, out, _ = run("show", f"https://youtu.be/{VID}")
    assert code == 0 and "[0:01]" in out


def test_unknown_command_is_argparse_error(run):
    code, _, _ = run("bogus")
    assert code == 2


# ---------------------------------------------------------------- file handling / temp cleanup
def test_default_dir_is_under_system_tempdir(tr, monkeypatch):
    import importlib.util, tempfile
    spec = importlib.util.spec_from_file_location("fresh", tr.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert fresh.default_dir().parent == __import__("pathlib").Path(tempfile.gettempdir())


def test_nothing_written_outside_given_dirs(run, tmp_path, docs, isolated_tmp):
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    run("parse", str(f), "--video", VID)
    written = {p.name for p in isolated_tmp.iterdir()}
    assert written == {f"{VID}.json"}                     # only the transcript, no stray temp files


def test_temp_store_removed_after_test(tmp_path, isolated_tmp, run, docs):
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    run("parse", str(f), "--video", VID)
    assert isolated_tmp.exists()
    import shutil
    shutil.rmtree(isolated_tmp)                            # cleanup is a plain directory delete
    assert not isolated_tmp.exists()


def test_out_parent_directories_are_created(run, tmp_path, docs):
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    out = tmp_path / "deep" / "er" / "doc.json"
    assert run("parse", str(f), "--out", str(out))[0] == 0 and out.is_file()


def test_saved_json_is_utf8_and_keeps_unicode(run, tmp_path):
    f = tmp_path / "u.txt"
    f.write_text("नमस्ते दुनिया\nこんにちは", encoding="utf-8")
    out = tmp_path / "u.json"
    run("parse", str(f), "--out", str(out))
    assert "नमस्ते" in out.read_text(encoding="utf-8")


# ---------------------------------------------------------------- fetch with a mocked library
def fake_api(monkeypatch, *, transcripts=None, list_exc=None, fetch_exc=None, raw=None, has_api=True):
    mod = types.ModuleType("youtube_transcript_api")

    class FakeTranscript:
        def __init__(self, code, generated):
            self.language_code, self.is_generated = code, generated

        def fetch(self):
            if fetch_exc:
                raise fetch_exc
            return types.SimpleNamespace(to_raw_data=lambda: raw)

    class FakeList(list):
        def find_transcript(self, langs):
            for t in self:
                if t.language_code in langs:
                    return t
            raise type("NoTranscriptFound", (Exception,), {})("none")

    class Api:
        def list(self, vid):
            if list_exc:
                raise list_exc
            return FakeList(FakeTranscript(c, g) for c, g in (transcripts or []))

        def fetch(self, *a, **k):  # presence only
            pass

    if not has_api:
        del Api.fetch
    mod.YouTubeTranscriptApi = Api
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", mod)


RAW = [{"text": "Hello  world", "start": 1.0, "duration": 2.0}, {"text": "  ", "start": 3.0, "duration": 1.0},
       {"text": "Second line", "start": 4.5, "duration": 1.5}]


def test_fetch_success_with_mock(run, monkeypatch, tmp_path):
    fake_api(monkeypatch, transcripts=[("en", True)], raw=RAW)
    out = tmp_path / "f.json"
    code, stdout, _ = run("fetch", f"https://youtu.be/{VID}", "--out", str(out))
    assert code == 0 and "auto-generated" in stdout
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert [s["text"] for s in doc["segments"]] == ["Hello world", "Second line"]   # blank dropped, cleaned
    assert doc["title"] == "Mock title" and doc["language"] == "en" and doc["duration_s"] == 6.0


def test_fetch_invalid_video(run):
    code, _, err = run("fetch", "https://example.com/watch?v=" + VID)
    assert code == 2 and "video ID" in err


def test_fetch_missing_dependency_message_is_safe(run, monkeypatch):
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", None)   # makes the import raise ImportError
    code, _, err = run("fetch", VID)
    assert code == 6 and "requirements.txt" in err and "break-system-packages" not in err and "sudo" not in err


def test_fetch_outdated_dependency(run, monkeypatch):
    fake_api(monkeypatch, has_api=False)
    code, _, err = run("fetch", VID)
    assert code == 6 and "too old" in err and "break-system-packages" not in err


def test_fetch_no_transcript_in_language(run, monkeypatch):
    fake_api(monkeypatch, transcripts=[("fr", False)], raw=RAW)
    code, _, err = run("fetch", VID, "--lang", "en")
    assert code == 4 and "fr (manual)" in err


def test_fetch_any_language_fallback(run, monkeypatch, tmp_path):
    fake_api(monkeypatch, transcripts=[("fr", False)], raw=RAW)
    assert run("fetch", VID, "--out", str(tmp_path / "f.json"))[0] == 0   # default "en,any"


def test_fetch_no_transcripts_at_all(run, monkeypatch):
    fake_api(monkeypatch, transcripts=[], raw=RAW)
    code, _, err = run("fetch", VID)
    assert code == 4 and "Available: none" in err


@pytest.mark.parametrize("name,code", [
    ("TranscriptsDisabled", 4), ("VideoUnavailable", 4), ("AgeRestricted", 4),
    ("RequestBlocked", 5), ("IpBlocked", 5), ("SomethingElse", 1),
])
def test_fetch_error_classification(run, monkeypatch, name, code):
    exc = type(name, (Exception,), {})("boom")
    fake_api(monkeypatch, list_exc=exc)
    assert run("fetch", VID)[0] == code


def test_fetch_network_error(run, monkeypatch):
    try:  # the real library raises requests' exceptions; fall back to the builtin if requests is absent
        import requests
        exc = requests.exceptions.ConnectionError("offline")
    except ImportError:
        exc = ConnectionError("offline")
    fake_api(monkeypatch, list_exc=exc)
    code, _, err = run("fetch", VID)
    assert code == 3 and "Cannot reach YouTube" in err


def test_fetch_empty_transcript(run, monkeypatch):
    fake_api(monkeypatch, transcripts=[("en", False)], raw=[{"text": " ", "start": 0, "duration": 1}])
    code, _, err = run("fetch", VID)
    assert code == 4 and "empty" in err


def test_fetch_failure_writes_nothing(run, monkeypatch, isolated_tmp):
    fake_api(monkeypatch, list_exc=type("TranscriptsDisabled", (Exception,), {})("x"))
    run("fetch", VID)
    assert not isolated_tmp.exists() or not list(isolated_tmp.iterdir())


def test_oembed_failure_never_breaks_fetch(tr, monkeypatch):
    import importlib.util, urllib.request
    spec = importlib.util.spec_from_file_location("fresh2", tr.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)

    def boom(*a, **k):
        raise OSError("no network")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert fresh.try_oembed(VID) == (None, None)


def test_oembed_only_contacts_youtube(tr, monkeypatch):
    import importlib.util, urllib.request
    spec = importlib.util.spec_from_file_location("fresh3", tr.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    seen = []

    def spy(url, timeout=None):
        seen.append(url)
        raise OSError("blocked in test")
    monkeypatch.setattr(urllib.request, "urlopen", spy)
    fresh.try_oembed(VID)
    assert len(seen) == 1 and seen[0].startswith("https://www.youtube.com/oembed?")


# ---------------------------------------------------------------- install hint
def test_requirements_hint_points_to_plugin_root_file(tr):
    hint = tr.requirements_hint()
    assert hint.endswith('requirements.txt"') or hint == "requirements.txt"
    if hint != "requirements.txt":
        from pathlib import Path
        assert Path(hint.strip('"')).is_file()


# ---------------------------------------------------------------- strict time-argument validation
@pytest.mark.parametrize("text,expected", [
    ("0", 0), ("59", 59), ("60", 60), ("754", 754), ("754.5", 754.5), ("3600", 3600),   # plain seconds may exceed 59
    ("0:00", 0), ("0:59", 59), ("1:00", 60), ("12:34", 754), ("75:30", 4530),            # M:SS, minutes unbounded
    ("1:02:03", 3723), ("0:00:00", 0), ("0:59:59", 3599), ("10:00:00", 36000),           # H:MM:SS boundaries
    ("0:01.500", 1.5), ("00:00:01,500", 1.5), ("00:00:01.500", 1.5), ("0:59.999", 59.999),  # fractional forms
    ("  12:34  ", 754),
])
def test_parse_time_arg_valid_and_boundary_values(tr, text, expected):
    assert tr.parse_time_arg(text) == pytest.approx(expected)


@pytest.mark.parametrize("bad", [
    "1:60", "0:60", "12:75", "1:99",                       # seconds out of range in M:SS
    "1:02:60", "1:60:00", "1:60:30", "0:00:99",            # seconds or minutes out of range in H:MM:SS
    "0:60.0", "1:02:59.5.5", "1:02:60,5",                  # out-of-range / malformed fractions
    "1:2:3:4", "1::30", ":30", "1:", "::", "1.5:30",       # wrong shape
    "abc", "", "  ", "nan", "inf", "Infinity", "1e3", "0x10", "٣:٠٠",  # not plain numbers (incl. non-ASCII digits)
])
def test_parse_time_arg_rejects_malformed(tr, bad):
    with pytest.raises(SystemExit) as exc:
        tr.parse_time_arg(bad)
    assert exc.value.code == tr.EXIT_BAD_INPUT


@pytest.mark.parametrize("neg", ["-1", "-0:30", "0:-30", "1:-02:03", "1:02:-3", "-inf", "-1.5", "-00:00:01,500"])
def test_parse_time_arg_negative_gets_negative_message(tr, neg, capsys):
    with pytest.raises(SystemExit) as exc:
        tr.parse_time_arg(neg)
    assert exc.value.code == tr.EXIT_BAD_INPUT and "negative" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--from", "--to"])
@pytest.mark.parametrize("bad", ["1:60", "1:02:60"])
def test_show_rejects_out_of_range_components(run, tmp_path, docs, flag, bad):
    *_, path = parse_file(run, tmp_path, docs.SRT, "a.srt")
    code, _, err = run("show", str(path), flag, bad)
    assert code == 2 and "Not a valid time" in err


@pytest.mark.parametrize("bad", ["1:60", "1:02:60"])
def test_link_rejects_out_of_range_components(run, tmp_path, docs, bad):
    *_, path = parse_file(run, tmp_path, docs.SRT, "a.srt")
    code, out, err = run("link", str(path), bad)
    assert code == 2 and "youtu.be" not in out


def test_link_and_show_still_accept_boundary_times(run, tmp_path, docs):
    *_, path = parse_file(run, tmp_path, docs.SRT, "a.srt")
    code, out, _ = run("link", str(path), "0:59", "1:00", "0:59:59")
    assert code == 0 and f"?t=59)" in out and f"?t=60)" in out and f"?t=3599)" in out
    assert run("show", str(path), "--from", "0:59", "--to", "1:59")[0] == 0
