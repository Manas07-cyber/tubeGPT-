"""Command-line argument validation and top-level error handling."""
import io
import os
import sys

import pytest

VID = "dQw4w9WgXcQ"
posix_only = pytest.mark.skipif(os.name != "posix", reason="needs POSIX named pipes")


@pytest.fixture
def saved(run, tmp_path, docs):
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    out = tmp_path / "doc.json"
    assert run("parse", str(f), "--video", VID, "--out", str(out))[0] == 0
    return str(out)


# ---------------------------------------------------------------- numeric options: bounds
@pytest.mark.parametrize("opt,bad", [
    ("--max", "0"), ("--max", "-1"), ("--max", "1001"), ("--max", "abc"), ("--max", "1.5"), ("--max", "1e3"),
    ("--max", ""), ("--max", "٣"), ("--max", "1_0"),
    ("--context", "-1"), ("--context", "51"), ("--context", "x"), ("--context", "2.5"), ("--context", "999999999999"),
])
def test_search_numeric_options_reject_bad_values(run, saved, opt, bad):
    code, _, err = run("search", saved, "rates", opt, bad)
    assert code == 2 and "error" in err.lower() and "Traceback" not in err


@pytest.mark.parametrize("bad", ["0", "-5", "1000001", "abc", "2.5", "", "99999999999999999999"])
def test_show_max_chars_rejects_bad_values(run, saved, bad):
    code, _, err = run("show", saved, "--max-chars", bad)
    assert code == 2 and "--max-chars" in err


@pytest.mark.parametrize("args", [("--max", "1"), ("--max", "1000"), ("--context", "0"), ("--context", "50")])
def test_search_boundary_values_work(run, saved, args):
    assert run("search", saved, "rates", *args)[0] == 0


@pytest.mark.parametrize("value", ["1", "80", "1000000"])
def test_show_max_chars_boundaries_work(run, saved, value):
    assert run("show", saved, "--max-chars", value)[0] == 0


def test_negative_messages_say_negative(run, saved):
    assert "negative" in run("search", saved, "x", "--context", "-1")[2]
    assert "negative" in run("search", saved, "x", "--max", "-3")[2]
    assert "negative" in run("show", saved, "--max-chars", "-3")[2]


def test_zero_gives_a_positive_requirement_message(run, saved):
    assert "between 1 and 1000" in run("search", saved, "x", "--max", "0")[2]


# ---------------------------------------------------------------- terms and times
def test_blank_only_search_terms_are_rejected(run, saved):
    code, _, err = run("search", saved, "   ", "")
    assert code == 2 and "non-empty" in err


def test_too_many_terms_are_rejected(run, saved):
    code, _, err = run("search", saved, *[f"t{i}" for i in range(51)])
    assert code == 2 and "Too many search terms" in err


def test_overlong_term_is_rejected(run, saved):
    code, _, err = run("search", saved, "x" * 501)
    assert code == 2 and "at most 500" in err


def test_blank_terms_are_ignored_when_another_is_valid(run, saved):
    code, out, _ = run("search", saved, "  ", "rates", "--context", "0")
    assert code == 0 and "Rates went up" in out


def test_link_prints_nothing_when_any_time_is_invalid(run, saved):
    code, out, err = run("link", saved, "5", "1:60", "10")
    assert code == 2 and out == "" and "Not a valid time" in err


def test_link_limits_the_number_of_times(run, saved):
    code, _, err = run("link", saved, *["5"] * 201)
    assert code == 2 and "Too many times" in err


def test_absurdly_large_times_are_rejected(run, saved):
    for value in ("99999999999", "99999:00:00", "10000001"):
        code, _, err = run("link", saved, value)
        assert code == 2 and "too large" in err


# ---------------------------------------------------------------- fetch options
@pytest.mark.parametrize("lang", ["en;rm", "english!", "e", "en,,x y", "../../x", "en," + ",".join(["fr"] * 25)])
def test_fetch_rejects_malformed_language_lists(run, lang):
    code, _, err = run("fetch", VID, "--lang", lang)
    assert code == 2 and "--lang" in err


@pytest.mark.parametrize("lang", ["en", "hi,en", "pt-BR", "en,any", "any", "zh-Hans"])
def test_fetch_accepts_normal_language_lists(tr, lang):
    assert tr.language_list(lang) == lang


def test_fetch_rejects_absurdly_long_video_argument(run):
    code, _, err = run("fetch", "x" * 3000)
    assert code == 2


# ---------------------------------------------------------------- file paths
def test_empty_out_path_is_rejected(run, tmp_path, docs):
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    code, _, err = run("parse", str(f), "--out", "")
    assert code == 2 and "--out" in err


def test_empty_file_argument_is_rejected(run):
    code, _, err = run("parse", "")
    assert code == 2


def test_folder_as_input_file_is_rejected(run, tmp_path):
    code, _, err = run("parse", str(tmp_path))
    assert code == 2 and "Cannot read" in err


@posix_only
def test_named_pipe_as_input_is_rejected_without_blocking(run, tmp_path):
    fifo = tmp_path / "pipe.txt"
    os.mkfifo(fifo)
    code, _, err = run("parse", str(fifo))
    assert code == 2 and "Cannot read" in err


def test_oversized_input_file_is_rejected(run, tr, tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "MAX_INPUT_BYTES", 64)
    f = tmp_path / "big.txt"
    f.write_text("word " * 100, encoding="utf-8")
    code, _, err = run("parse", str(f))
    assert code == 2 and "too large" in err


def test_stdin_input_works_and_is_size_limited(run, tr, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdin", io.StringIO("first line\nsecond line\n"))
    code, out, _ = run("parse", "-", "--out", str(tmp_path / "s.json"))
    assert code == 0 and "segments: 2" in out
    monkeypatch.setattr(tr, "MAX_INPUT_BYTES", 10)
    monkeypatch.setattr(sys, "stdin", io.StringIO("x" * 100))
    code, _, err = run("parse", "-", "--out", str(tmp_path / "s2.json"))
    assert code == 2 and "too large" in err


def test_too_many_segments_in_input_are_rejected(run, tr, tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "MAX_SEGMENTS", 2)
    f = tmp_path / "n.txt"
    f.write_text("a\nb\nc\n", encoding="utf-8")
    code, _, err = run("parse", str(f))
    assert code == 2 and "more than 2 segments" in err


def test_overlong_title_is_rejected(run, tmp_path, docs):
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    assert run("parse", str(f), "--title", "t" * 301)[0] == 2


def test_nul_byte_in_source_is_handled(run):
    code, _, err = run("show", "a\x00b")
    assert code == 2 and "Traceback" not in err


# ---------------------------------------------------------------- help, logging and unexpected errors
@pytest.mark.parametrize("argv", [["--help"], ["fetch", "--help"], ["parse", "--help"], ["show", "--help"],
                                  ["search", "--help"], ["link", "--help"], ["cleanup", "--help"]])
def test_help_output_works(run, argv):
    code, out, _ = run(*argv)
    assert code == 0 and "usage:" in out


def test_help_mentions_ranges(run):
    out = run("search", "--help")[1]
    assert "0-50" in out and "1-1000" in out


def test_no_command_is_an_argparse_error(run):
    assert run()[0] == 2


def test_verbose_logs_events_but_never_transcript_text(run, tmp_path, docs):
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    code, out, err = run("-v", "parse", str(f), "--video", VID)
    assert code == 0
    assert "event=cleanup_skipped" in err or "event=cleanup" in err
    assert "interest rates" not in err and "welcome" not in err


def test_default_run_is_quiet_on_stderr(run, tmp_path, docs):
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    code, _, err = run("parse", str(f), "--video", VID)
    assert code == 0 and err == ""


def test_unexpected_exceptions_become_a_controlled_exit(run, tr, saved, monkeypatch):
    def boom(src):
        raise RuntimeError("secret internal detail /home/someone/file")
    monkeypatch.setattr(tr, "load_doc", boom)
    code, _, err = run("show", saved)
    assert code == 1 and "Unexpected internal error (RuntimeError)" in err
    assert "Traceback" not in err and "/home/someone" not in err


def test_verbose_shows_the_traceback_for_unexpected_errors(run, tr, saved, monkeypatch):
    monkeypatch.setattr(tr, "load_doc", lambda src: (_ for _ in ()).throw(RuntimeError("x")))
    code, _, err = run("-v", "show", saved)
    assert code == 1 and "Traceback" in err


def test_keyboard_interrupt_exits_130(run, tr, saved, monkeypatch):
    monkeypatch.setattr(tr, "load_doc", lambda src: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert run("show", saved)[0] == 130


def test_broken_pipe_is_not_reported_as_a_crash(run, tr, saved, monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())                 # restored by monkeypatch afterwards
    monkeypatch.setattr(tr, "load_doc", lambda src: (_ for _ in ()).throw(BrokenPipeError()))
    code, _, err = run("show", saved)
    assert code == 1 and "Traceback" not in err
