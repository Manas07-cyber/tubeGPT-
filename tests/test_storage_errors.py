"""Failure injection for the storage code: the paths that only run when the operating system says no.

These are the branches a security gate must not leave untested, because they decide what happens when a file
vanishes mid-scan, cannot be listed, opened, re-permissioned or removed. Each test checks the visible outcome
(exit code, nothing deleted, no leftover temp file), not just that a line ran.
"""
import errno
import json
import logging
import os
import stat
import tempfile
import time

import pytest

VID = "dQw4w9WgXcQ"
OTHER = "aaaaaaaaaaa"
HOUR = 3600
DOC = {"video_id": VID, "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}], "has_timestamps": True}
posix_only = pytest.mark.skipif(os.name != "posix", reason="needs POSIX permissions and user IDs")


def make(path, age_h=0.0, perm=None):
    path.write_text(json.dumps(DOC), encoding="utf-8")
    t = time.time() - age_h * HOUR
    os.utime(path, (t, t))
    if perm is not None:
        os.chmod(path, perm)
    return path


@pytest.fixture
def logs(tr):
    """Messages logged by the script. A handler on its own logger works whatever earlier tests did to propagation."""
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


# ----------------------------------------------------------------------------- secure_store_files
@posix_only
def test_securing_files_reports_a_folder_that_cannot_be_listed(tr, isolated_tmp, monkeypatch, capsys):
    isolated_tmp.mkdir(mode=0o700)
    monkeypatch.setattr(os, "listdir", lambda p: (_ for _ in ()).throw(PermissionError(errno.EACCES, "denied")))
    assert exit_code(tr.secure_store_files, isolated_tmp) == tr.EXIT_STORAGE
    err = capsys.readouterr().err
    assert "Permission was denied" in err and "check the permissions of saved transcripts" in err
    assert str(isolated_tmp) not in err, "error messages must not reveal paths"


@posix_only
def test_a_file_removed_while_securing_is_skipped_quietly(tr, isolated_tmp, monkeypatch):
    isolated_tmp.mkdir(mode=0o700)
    kept = make(isolated_tmp / f"{VID}.json", perm=0o644)
    make(isolated_tmp / f"{OTHER}.json", perm=0o644)
    real = tr._open_regular

    def vanishing(path, *a, **k):
        if path.name == f"{OTHER}.json":
            raise FileNotFoundError(errno.ENOENT, "gone")
        return real(path, *a, **k)

    monkeypatch.setattr(tr, "_open_regular", vanishing)
    assert tr.secure_store_files(isolated_tmp) == 1
    assert stat.S_IMODE(os.stat(kept).st_mode) == 0o600


@posix_only
def test_an_unexpected_open_error_while_securing_is_a_controlled_storage_error(tr, isolated_tmp, monkeypatch):
    isolated_tmp.mkdir(mode=0o700)
    make(isolated_tmp / f"{VID}.json", perm=0o644)
    monkeypatch.setattr(tr, "_open_regular", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EIO, "disk error")))
    assert exit_code(tr.secure_store_files, isolated_tmp) == tr.EXIT_STORAGE


@posix_only
def test_a_failing_chmod_is_a_controlled_error_and_closes_the_descriptor(tr, isolated_tmp, monkeypatch):
    isolated_tmp.mkdir(mode=0o700)
    make(isolated_tmp / f"{VID}.json", perm=0o644)
    closed = []
    real_close = os.close
    monkeypatch.setattr(os, "close", lambda fd: (closed.append(fd), real_close(fd))[1])
    monkeypatch.setattr(os, "fchmod", lambda fd, mode: (_ for _ in ()).throw(PermissionError(errno.EPERM, "no")))
    assert exit_code(tr.secure_store_files, isolated_tmp) == tr.EXIT_STORAGE
    assert closed, "the descriptor must be closed even when chmod fails"


def test_securing_does_nothing_on_a_platform_without_posix_permissions(tr, isolated_tmp, monkeypatch):
    isolated_tmp.mkdir()
    target = make(isolated_tmp / f"{VID}.json", perm=0o644 if os.name == "posix" else None)
    before = os.stat(target).st_mode
    monkeypatch.setattr(tr, "_IS_POSIX", False)
    assert tr.secure_store_files(isolated_tmp) == 0
    tr._tighten_dir(isolated_tmp)
    assert os.stat(target).st_mode == before


@posix_only
def test_a_folder_that_changes_owner_between_checks_is_refused_when_tightening(tr, isolated_tmp, monkeypatch):
    isolated_tmp.mkdir(mode=0o755)
    monkeypatch.setattr(tr, "_owned_by_me", lambda info: False)
    assert exit_code(tr._tighten_dir, isolated_tmp) == tr.EXIT_STORAGE
    assert stat.S_IMODE(os.stat(isolated_tmp).st_mode) == 0o755, "a folder of someone else is never re-permissioned"


# ----------------------------------------------------------------------------- _remove_if_unchanged
def scanned(tr, store, name, age_h=900):
    path = make(store / name, age_h=age_h)
    entries = {e.name: e for e in tr._scan_store(store, None)}
    return path, entries[name]


def test_a_file_that_vanished_before_the_recheck_is_not_an_error(tr, isolated_tmp):
    isolated_tmp.mkdir()
    path, entry = scanned(tr, isolated_tmp, f"{VID}.json")
    path.unlink()
    assert tr._remove_if_unchanged(isolated_tmp, None, entry, time.time(), 1) is False


def test_a_file_that_cannot_be_re_checked_is_kept_and_reported(tr, isolated_tmp, monkeypatch, logs):
    isolated_tmp.mkdir()
    path, entry = scanned(tr, isolated_tmp, f"{VID}.json")
    monkeypatch.setattr(tr, "_stat_entry", lambda *a: (_ for _ in ()).throw(PermissionError(errno.EACCES, "no")))
    assert tr._remove_if_unchanged(isolated_tmp, None, entry, time.time(), 1) is False
    assert path.exists() and any("Could not re-check" in m for m in logs)


def test_a_file_that_is_too_young_at_the_recheck_is_kept(tr, isolated_tmp):
    isolated_tmp.mkdir()
    path, entry = scanned(tr, isolated_tmp, f"{VID}.json", age_h=1)
    assert tr._remove_if_unchanged(isolated_tmp, None, entry, time.time(), 5 * HOUR) is False
    assert path.exists()


def test_a_file_removed_between_the_recheck_and_the_delete_is_not_an_error(tr, isolated_tmp, monkeypatch):
    isolated_tmp.mkdir()
    path, entry = scanned(tr, isolated_tmp, f"{VID}.json")
    monkeypatch.setattr(os, "unlink", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError(errno.ENOENT, "x")))
    assert tr._remove_if_unchanged(isolated_tmp, None, entry, time.time(), 1) is False


def test_a_refused_delete_is_reported_and_the_file_stays(tr, isolated_tmp, monkeypatch, logs):
    isolated_tmp.mkdir()
    path, entry = scanned(tr, isolated_tmp, f"{VID}.json")
    monkeypatch.setattr(os, "unlink", lambda *a, **k: (_ for _ in ()).throw(PermissionError(errno.EACCES, "no")))
    assert tr._remove_if_unchanged(isolated_tmp, None, entry, time.time(), 1) is False
    assert path.exists() and any("Could not remove" in m for m in logs)


def test_scanning_skips_an_entry_that_vanishes_during_the_scan(tr, isolated_tmp, monkeypatch):
    isolated_tmp.mkdir()
    make(isolated_tmp / f"{VID}.json")
    make(isolated_tmp / f"{OTHER}.json")
    real = tr._stat_entry

    def flaky(store, dirfd, name):
        if name == f"{VID}.json":
            raise FileNotFoundError(errno.ENOENT, "gone")
        return real(store, dirfd, name)

    monkeypatch.setattr(tr, "_stat_entry", flaky)
    assert [e.name for e in tr._scan_store(isolated_tmp, None)] == [f"{OTHER}.json"]


def test_a_failing_scan_stops_cleanup_without_deleting_anything(tr, isolated_tmp, monkeypatch, logs):
    isolated_tmp.mkdir()
    old = make(isolated_tmp / f"{VID}.json", age_h=900)
    monkeypatch.setattr(tr, "_scan_store", lambda *a: (_ for _ in ()).throw(OSError(errno.EIO, "io")))
    result = tr.cleanup_store(isolated_tmp)
    assert old.exists() and result["expired"] == 0 and any("Could not clean" in m for m in logs)


# ----------------------------------------------------------------------------- write_json_atomic
@pytest.mark.parametrize("bad", [{"x": {1, 2}}, {"x": float("nan")}, {"x": float("inf")}])
def test_a_document_that_cannot_be_encoded_is_refused_and_nothing_is_written(tr, tmp_path, bad):
    target = tmp_path / f"{VID}.json"
    assert exit_code(tr.write_json_atomic, target, bad) == tr.EXIT_BAD_INPUT
    assert list(tmp_path.iterdir()) == []


def test_a_temp_file_that_cannot_be_removed_after_a_failed_write_does_not_hide_the_real_error(
        tr, tmp_path, monkeypatch, logs):
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(OSError(errno.ENOSPC, "disk full")))
    monkeypatch.setattr(os, "unlink", lambda p: (_ for _ in ()).throw(PermissionError(errno.EACCES, "no")))
    assert exit_code(tr.write_json_atomic, tmp_path / f"{VID}.json", DOC) == tr.EXIT_STORAGE
    assert any("temp_cleanup_failed" in m for m in logs)


# ----------------------------------------------------------------------------- store_file, scrub
def test_store_file_has_a_second_check_that_does_not_depend_on_the_name_pattern(tr, monkeypatch):
    """Defence in depth: even if the name pattern were loosened by mistake, a path outside the store is refused."""
    import re
    monkeypatch.setattr(tr, "STORE_FILE_RE", re.compile(r".+"))
    assert exit_code(tr.store_file, "../escape.json") == tr.EXIT_BAD_INPUT
    assert exit_code(tr.store_file, "sub/escape.json") == tr.EXIT_BAD_INPUT


def test_scrub_still_hides_folders_when_the_working_folder_is_gone(tr, monkeypatch):
    monkeypatch.setattr(os, "getcwd", lambda: (_ for _ in ()).throw(FileNotFoundError(errno.ENOENT, "deleted")))
    text = tr.scrub(f"failed at {tempfile.gettempdir()}/secret.json")
    assert tempfile.gettempdir() not in text and text == "failed at <folder>/secret.json"
