"""Edge paths of the storage, reading and fetch code that the main suites do not reach.

Each test pins an observable outcome: an exit code, a message, a file that stays or is left alone.
"""
import builtins
import errno
import json
import logging
import os
import pathlib
import stat
import sys
import time
import types

import pytest

VID = "dQw4w9WgXcQ"
DOC = {"video_id": VID, "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}], "has_timestamps": True}
posix_only = pytest.mark.skipif(os.name != "posix", reason="needs POSIX permissions, links and user IDs")


@pytest.fixture
def logs(tr):
    messages = []

    class Collect(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    handler, previous = Collect(level=logging.DEBUG), tr.log.level
    tr.log.addHandler(handler)
    tr.log.setLevel(logging.DEBUG)
    yield messages
    tr.log.removeHandler(handler)
    tr.log.setLevel(previous)


def exit_code(call, *args, **kwargs):
    with pytest.raises(SystemExit) as exc:
        call(*args, **kwargs)
    return exc.value.code


def make(path, perm=None):
    path.write_text(json.dumps(DOC), encoding="utf-8")
    if perm is not None:
        os.chmod(path, perm)
    return path


# ------------------------------------------------------------------------------------------- split_text
def test_split_text_returns_short_text_unchanged_and_uses_the_loader_limit_by_default(tr):
    assert tr.split_text("short") == ["short"]
    assert tr.split_text("x" * tr.MAX_SEGMENT_CHARS) == ["x" * tr.MAX_SEGMENT_CHARS]
    pieces = tr.split_text("y" * (tr.MAX_SEGMENT_CHARS + 1))
    assert len(pieces) == 2 and "".join(pieces) == "y" * (tr.MAX_SEGMENT_CHARS + 1)
    assert all(len(p) <= tr.MAX_SEGMENT_CHARS for p in pieces)


def test_split_text_never_loses_words_or_exceeds_an_explicit_limit(tr):
    text = "alpha beta gamma delta epsilon " * 20
    pieces = tr.split_text(text, 50)
    assert all(len(p) <= 50 for p in pieces)
    assert " ".join(" ".join(pieces).split()) == " ".join(text.split())


# ------------------------------------------------------------------------------------------- _touch
def test_touch_falls_back_to_the_path_when_descriptors_are_not_supported(tr, tmp_path, monkeypatch):
    path = make(tmp_path / f"{VID}.json")
    os.utime(path, (1_000_000, 1_000_000))
    monkeypatch.setattr(os, "supports_fd", set())
    fd = os.open(path, os.O_RDONLY)
    try:
        tr._touch(fd, path)
    finally:
        os.close(fd)
    assert os.stat(path).st_mtime > time.time() - 60


def test_a_failed_touch_is_logged_and_never_stops_the_read(tr, tmp_path, monkeypatch, logs):
    path = make(tmp_path / f"{VID}.json")
    monkeypatch.setattr(os, "utime", lambda *a, **k: (_ for _ in ()).throw(PermissionError(errno.EPERM, "no")))
    monkeypatch.setattr(os, "supports_fd", set())
    tr._touch(0, path)
    assert any("touch_failed" in m for m in logs)


# ------------------------------------------------------------------------------------------- _dir_fd
@posix_only
def test_a_folder_of_another_user_gets_no_directory_handle(tr, tmp_path, monkeypatch):
    if tr._dir_fd(tmp_path) is None:
        pytest.skip("this platform has no directory file descriptors")
    monkeypatch.setattr(tr, "_owned_by_me", lambda info: False)
    with pytest.raises(tr.UnsafeStoreEntry, match="another user"):
        tr._dir_fd(tmp_path)


def test_without_directory_descriptors_the_path_based_scan_is_used(tr, tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_IS_POSIX", False)
    assert tr._dir_fd(tmp_path) is None


# ------------------------------------------------------------------------------------------- make_api
def test_make_api_falls_back_when_the_client_rejects_a_session(tr):
    class Strict:
        def __init__(self, proxy_config=None, http_client=None):
            if http_client is not None:
                raise TypeError("this client takes no session")
            self.plain = True

    assert tr.make_api(Strict).plain


def test_make_api_falls_back_when_requests_is_not_installed(tr, monkeypatch):
    monkeypatch.setitem(sys.modules, "requests", None)  # makes "import requests" raise ImportError

    class Api:
        def __init__(self, proxy_config=None, http_client=None):
            self.client = http_client

    assert tr.make_api(Api).client is None


# ------------------------------------------------------------------------------------------- store_lock
def test_a_lock_that_cannot_be_taken_in_time_is_reported_as_not_held(tr, isolated_tmp, logs):
    isolated_tmp.mkdir(mode=0o700)
    with tr.store_lock(isolated_tmp) as first:
        assert first is True
        started = time.monotonic()
        with tr.store_lock(isolated_tmp, timeout=0.2) as second:
            assert second is False
        assert time.monotonic() - started < 5
    assert any("lock_timeout" in m for m in logs)
    with tr.store_lock(isolated_tmp) as again:   # released properly: it can be taken again
        assert again is True


def test_no_lock_file_is_created_in_an_unmarked_custom_folder(tr, tmp_path, monkeypatch):
    folder = tmp_path / "mine"
    folder.mkdir()
    (folder / "notes.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setenv("TUBEGPT_STORAGE_DIR", str(folder))
    with tr.store_lock(folder) as held:
        assert held is False
    assert sorted(p.name for p in folder.iterdir()) == ["notes.txt"]


# ------------------------------------------------------------------------------------------- _open_regular
@posix_only
def test_the_windows_style_link_check_also_refuses_links(tr, tmp_path, monkeypatch):
    target = make(tmp_path / "target.json")
    link = tmp_path / f"{VID}.json"
    link.symlink_to(target)
    monkeypatch.setattr(tr, "_IS_POSIX", False)   # as on Windows, where O_NOFOLLOW does not exist
    with pytest.raises(tr.UnsafeStoreEntry, match="symbolic link"):
        tr._open_regular(link)


# ------------------------------------------------------------------------------------------- read_input_text
def test_a_file_that_cannot_be_opened_is_a_controlled_error(tr, tmp_path, monkeypatch, capsys):
    path = tmp_path / "caps.srt"
    path.write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n", encoding="utf-8")
    real_open = builtins.open

    def refuse(file, *args, **kwargs):
        if os.fspath(file) == str(path):
            raise PermissionError(errno.EACCES, "denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", refuse)
    assert exit_code(tr.read_input_text, str(path)) == tr.EXIT_BAD_INPUT
    err = capsys.readouterr().err
    assert "PermissionError" in err and "Traceback" not in err


# ------------------------------------------------------------------------------------------- load_doc
def test_a_path_that_cannot_be_inspected_is_treated_as_a_name_not_a_crash(tr, monkeypatch, capsys):
    def broken(self, *a, **k):
        raise OSError(errno.EIO, "io error")

    monkeypatch.setattr(pathlib.Path, "is_file", broken)
    assert exit_code(tr.load_doc, "definitely-not-a-video") == tr.EXIT_BAD_INPUT
    assert "No saved transcript" in capsys.readouterr().err


@posix_only
def test_a_loose_transcript_that_appears_after_the_folder_check_is_tightened_before_reading(
        tr, isolated_tmp, monkeypatch):
    """ensure_store tightens existing files, but a file can arrive afterwards; the read tightens it too."""
    isolated_tmp.mkdir(mode=0o700)
    target = make(isolated_tmp / f"{VID}.json")
    real_ensure = tr.ensure_store

    def late_arrival(create=True):
        store = real_ensure(create)
        os.chmod(target, 0o644)
        return store

    monkeypatch.setattr(tr, "ensure_store", late_arrival)
    doc, _ = tr.load_doc(VID)
    assert doc["video_id"] == VID and stat.S_IMODE(os.stat(target).st_mode) == 0o600


@posix_only
def test_a_pipe_that_replaces_a_named_file_is_refused_without_blocking(tr, tmp_path, monkeypatch, capsys):
    fifo = tmp_path / "named.json"
    os.mkfifo(fifo)
    monkeypatch.setattr(pathlib.Path, "is_file", lambda self, *a, **k: True)   # the file "was" regular a moment ago
    assert exit_code(tr.load_doc, str(fifo)) == tr.EXIT_BAD_INPUT
    assert "not a regular file" in capsys.readouterr().err


def test_an_os_error_while_reading_a_named_file_is_a_controlled_error(tr, tmp_path, monkeypatch, capsys):
    path = make(tmp_path / "doc.json")
    monkeypatch.setattr(tr, "_read_validated", lambda *a: (_ for _ in ()).throw(OSError(errno.EIO, "io")))
    assert exit_code(tr.load_doc, str(path)) == tr.EXIT_BAD_INPUT
    err = capsys.readouterr().err
    assert "OSError" in err and str(tmp_path) not in err


# ------------------------------------------------------------------------------------------- _read_validated
def test_a_file_reported_as_too_large_is_refused_before_reading(tr):
    fd = os.open(os.devnull, os.O_RDONLY)
    try:
        info = types.SimpleNamespace(st_size=tr.MAX_DOC_BYTES + 1)
        assert exit_code(tr._read_validated, fd, info, "big.json") == tr.EXIT_BAD_INPUT
    finally:
        os.close(fd)


def test_a_file_that_grows_past_the_limit_while_being_read_is_refused(tr, tmp_path, monkeypatch, capsys):
    path = tmp_path / "grows.json"
    path.write_bytes(b" " * 200)
    monkeypatch.setattr(tr, "MAX_DOC_BYTES", 100)
    fd = os.open(path, os.O_RDONLY)
    try:
        info = types.SimpleNamespace(st_size=10)    # stale size: the file was small when it was checked
        assert exit_code(tr._read_validated, fd, info, "grows.json") == tr.EXIT_BAD_INPUT
    finally:
        os.close(fd)
    assert "too large" in capsys.readouterr().err


@pytest.mark.parametrize("error, expected", [
    (OverflowError("int too large to convert to float"), "number that is too large"),
    (RecursionError("maximum recursion depth"), "nested too deeply"),
])
def test_unexpected_numeric_or_depth_errors_are_controlled(tr, tmp_path, monkeypatch, capsys, error, expected):
    path = make(tmp_path / "doc.json")
    monkeypatch.setattr(tr, "validate_doc", lambda doc: (_ for _ in ()).throw(error))
    assert exit_code(tr.load_doc, str(path)) == tr.EXIT_BAD_INPUT
    assert expected in capsys.readouterr().err


def test_deeply_nested_json_is_refused_not_a_crash(tr, tmp_path, capsys):
    path = tmp_path / "deep.json"
    path.write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
    assert exit_code(tr.load_doc, str(path)) == tr.EXIT_BAD_INPUT
    assert "Traceback" not in capsys.readouterr().err


# ------------------------------------------------------------------------------------------- parsing
def test_out_of_order_raw_captions_are_sorted_not_dropped(tr):
    raw = [{"text": "second", "start": 5.0, "duration": 1.0}, {"text": "first", "start": 1.0, "duration": 1.0},
           {"text": "third", "start": 9.0, "duration": 1.0}]
    segments, skipped = tr.segments_from_raw(raw)
    assert [s["text"] for s in segments] == ["first", "second", "third"] and skipped == 0


def test_text_before_the_first_timestamp_is_reported_not_silently_dropped(tr):
    warnings = []
    segments = tr.parse_panel(["Transcript", "Video title", "0:01 hello", "0:05 world"], warnings)
    assert [s["text"] for s in segments] == ["hello", "world"]
    assert warnings and "Ignored 2 line(s) before the first timestamp" in warnings[0]


def test_two_captions_with_the_same_timestamp_are_both_kept(tr):
    segments, _ = tr.parse_transcript_text("0:05 first\n0:05 second\n0:09 third\n")
    assert [s["text"] for s in segments] == ["first", "second", "third"]


def test_an_unreadable_timing_line_is_counted_and_reported(tr):
    srt = ("1\n00:00:01,000 --> 00:00:02,000\nfine\n\n2\n00:99:99,000 --> 00:00:04,000\nlost text\n\n"
           "3\n00:00:05,000 --> 00:00:06,000\nalso fine\n")
    warnings = []
    segments = tr.parse_cues(srt, warnings)
    assert [s["text"] for s in segments] == ["fine", "also fine"]
    assert any("malformed timestamps" in w for w in warnings), "dropped cues must never be silent"


def test_only_a_cue_with_the_same_start_and_the_same_text_counts_as_a_duplicate(tr):
    srt = ("1\n00:00:05,000 --> 00:00:06,000\nNo.\n\n2\n00:00:05,000 --> 00:00:06,000\nNo.\n\n"
           "3\n00:00:09,000 --> 00:00:10,000\nNo.\n")
    warnings = []
    segments = tr.parse_cues(srt, warnings)
    assert [(s["start"], s["text"]) for s in segments] == [(5.0, "No."), (9.0, "No.")]
    assert any("Dropped 1 caption cue" in w for w in warnings)


def test_caption_timing_lines_that_are_all_unreadable_are_an_error_not_plain_text(tr):
    with pytest.raises(tr.ParseError, match="none could be read"):
        tr.parse_transcript_text("00:99:99,000 --> 00:99:99,500\nhello\n\n00:99:99,600 --> 00:99:99,900\nworld\n")


def test_network_errors_are_recognised_even_without_the_requests_package(tr, monkeypatch):
    monkeypatch.setitem(sys.modules, "requests", None)
    code, message = tr.classify_error(ConnectionError("unreachable"))
    assert code == tr.EXIT_NETWORK and "Cannot reach YouTube" in message


def test_cleanup_still_runs_when_the_lock_cannot_be_taken(tr, isolated_tmp, monkeypatch, run):
    import contextlib
    isolated_tmp.mkdir(mode=0o700)
    old = make(isolated_tmp / f"{VID}.json")
    os.utime(old, (1_000_000, 1_000_000))

    @contextlib.contextmanager
    def never_locked(store, **kwargs):
        yield False

    monkeypatch.setattr(tr, "store_lock", never_locked)
    code, out, _ = run("cleanup")
    assert code == 0 and "Removed 1 expired" in out and not old.exists()


def test_a_store_created_by_another_process_at_the_same_moment_is_used_after_the_usual_checks(
        tr, isolated_tmp, monkeypatch):
    """Two first-time runs race to create the folder: the loser must adopt it, safely, and not fail."""
    real_mkdir = pathlib.Path.mkdir

    def lose_the_race(self, *args, **kwargs):
        real_mkdir(self, *args, **kwargs)           # the other process got there first...
        raise FileExistsError(errno.EEXIST, "exists")  # ...so ours reports that it already exists

    monkeypatch.setattr(pathlib.Path, "mkdir", lose_the_race)
    store = tr.ensure_store(create=True)
    assert store == isolated_tmp and store.is_dir()
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(store).st_mode) == 0o700


@posix_only
def test_the_race_winner_cannot_hand_us_a_symbolic_link_as_the_store(tr, isolated_tmp, tmp_path, monkeypatch, capsys):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def attacker_wins(self, *args, **kwargs):
        os.symlink(elsewhere, self)                  # a link planted exactly when we meant to create the folder
        raise FileExistsError(errno.EEXIST, "exists")

    monkeypatch.setattr(pathlib.Path, "mkdir", attacker_wins)
    assert exit_code(tr.ensure_store, True) == tr.EXIT_STORAGE
    assert "symbolic link" in capsys.readouterr().err and list(elsewhere.iterdir()) == []


def test_a_timing_line_with_garbage_after_the_arrow_is_counted_as_skipped(tr):
    srt = "1\n00:00:01,000 --> 00:00:02,000\nkept\n\n2\n00:00:03,000 --> soon\nlost\n\n3\n00:00:05,000 --> 00:00:06,000\nkept too\n"
    warnings = []
    assert [s["text"] for s in tr.parse_cues(srt, warnings)] == ["kept", "kept too"]
    assert any("Skipped 1 caption cue" in w for w in warnings)


def test_a_timestamp_with_no_text_after_it_is_dropped_but_its_neighbours_are_kept(tr):
    segments = tr.parse_panel(["0:01", "0:02 first words", "0:05", "0:06 more words"])
    assert [(s["start"], s["text"]) for s in segments] == [(2.0, "first words"), (6.0, "more words")]


def test_a_single_character_folder_is_not_replaced_in_error_text(tr, monkeypatch):
    """A home folder of '/' would otherwise turn every slash in a message into '<folder>'."""
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: pathlib.Path("/")))
    assert tr.scrub("opened /a/b/c failed") == "opened /a/b/c failed"


def test_the_rename_retry_gives_up_after_the_configured_number_of_attempts(tr, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "replace", lambda s, d: (calls.append(1), (_ for _ in ()).throw(PermissionError(13, "x")))[1])
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    monkeypatch.setattr(tr, "_IS_POSIX", False)
    with pytest.raises(PermissionError):
        tr._replace("a", "b")
    assert len(calls) == tr.REPLACE_ATTEMPTS
