"""Windows ownership and cleanup safety.

Two kinds of test live here.

* Portable tests (run everywhere) replace the thin Win32 wrappers with fakes and check the DECISIONS the code
  makes: unknown ownership is never trusted, a deletion is refused unless the open file is exactly the scanned one.
* Windows-only tests (``on_windows``) call the real Win32 functions: real owners, real handle-based deletion,
  a real hard link, symbolic link and junction, a real sharing violation. They are the only evidence for the
  Windows behaviour and run only on the Windows CI jobs; on Linux and macOS they are skipped with a reason.
"""
import os
import stat
import subprocess
import time
import types

import pytest

VID = "dQw4w9WgXcQ"
OTHER = "aaaaaaaaaaa"
HOUR = 3600
DOC = '{"video_id": "%s", "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}], "has_timestamps": true}'
SYSTEM32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")   # full paths: no PATH lookup
ICACLS = os.path.join(SYSTEM32, "icacls.exe")
CMD = os.path.join(SYSTEM32, "cmd.exe")
on_windows = pytest.mark.skipif(os.name != "nt", reason="exercises the real Win32 functions; runs on the Windows CI jobs only")


def make(path, age_h=0.0, text=None):
    path.write_text(text or DOC % VID, encoding="utf-8")
    stamp = time.time() - age_h * HOUR
    os.utime(path, (stamp, stamp))
    return path


def scan_one(tr, folder, name):
    return next(e for e in tr._scan_store(folder, None) if e.name == name)


@pytest.fixture
def as_windows(tr, monkeypatch):
    """Make the portable decision code behave as on Windows, with fake Win32 functions that act on the real file."""
    monkeypatch.setattr(tr, "_IS_POSIX", False)
    monkeypatch.delattr(os, "getuid", raising=False)
    state = types.SimpleNamespace(mine=True, marked=[], reparse=False, owner_checks=[])

    def fake_owner(target):
        state.owner_checks.append(target)
        return "S-1-5-21-1" if state.mine else "S-1-5-21-999"

    monkeypatch.setattr(tr, "_win_owner_sid", fake_owner)
    monkeypatch.setattr(tr, "_win_my_sids", lambda: frozenset({"S-1-5-21-1"}))
    monkeypatch.setattr(tr, "_win_open_for_delete", lambda path: os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)))
    monkeypatch.setattr(tr, "_win_pin_dir", lambda path: os.open(path, os.O_RDONLY))

    def mark(fd):
        state.marked.append(fd)

    monkeypatch.setattr(tr, "_win_mark_delete", mark)
    real_fstat = os.fstat

    def fstat(fd):
        info = real_fstat(fd)
        if not state.reparse:
            return info
        fields = {n: getattr(info, n) for n in dir(info) if n.startswith("st_")}
        fields["st_file_attributes"] = tr._WIN_REPARSE
        return types.SimpleNamespace(**fields)

    monkeypatch.setattr(os, "fstat", fstat)
    return state


# ------------------------------------------------------------------------------ ownership decisions
def test_unknown_ownership_is_never_trusted(tr, tmp_path, monkeypatch):
    path = make(tmp_path / f"{VID}.json")
    assert tr._windows_owned_by_me(None) is False

    def broken(target):
        raise OSError(5, "access denied")

    monkeypatch.setattr(tr, "_win_owner_sid", broken)
    assert tr._windows_owned_by_me(path) is False, "an owner that cannot be read must not count as mine"


@pytest.mark.parametrize("problem", [OSError(5, "x"), ValueError("x"), AttributeError("x"), ImportError("x"), TypeError("x")])
def test_every_failure_to_read_the_owner_means_not_mine(tr, tmp_path, monkeypatch, problem):
    monkeypatch.setattr(tr, "_win_my_sids", lambda: frozenset({"S-1"}))

    def broken(target):
        raise problem

    monkeypatch.setattr(tr, "_win_owner_sid", broken)
    assert tr._windows_owned_by_me(tmp_path) is False


def test_only_a_sid_of_the_current_user_counts(tr, tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_win_my_sids", lambda: frozenset({"S-1-5-21-1", "S-1-5-32-544"}))
    for sid, expected in (("S-1-5-21-1", True), ("S-1-5-32-544", True), ("S-1-5-21-2", False), ("S-1-5-18", False), ("", False)):
        monkeypatch.setattr(tr, "_win_owner_sid", lambda target, sid=sid: sid)
        assert tr._windows_owned_by_me(tmp_path) is expected, sid


def test_a_stat_result_alone_never_proves_ownership_without_user_ids(tr, tmp_path, monkeypatch):
    info = os.stat(tmp_path)
    monkeypatch.delattr(os, "getuid", raising=False)
    assert tr._owned_by_me(info) is False


@pytest.mark.skipif(os.name != "posix", reason="POSIX user IDs")
def test_posix_ownership_still_compares_user_ids(tr, tmp_path, monkeypatch):
    info = os.stat(tmp_path)
    assert tr._is_mine(info, tmp_path) is True
    monkeypatch.setattr(os, "getuid", lambda: info.st_uid + 1)
    assert tr._is_mine(info, tmp_path) is False


def test_the_windows_check_reads_the_open_descriptor_not_just_the_path(tr, tmp_path, as_windows):
    path = make(tmp_path / f"{VID}.json")
    fd, _ = tr._open_regular(path)
    os.close(fd)
    assert any(isinstance(target, int) for target in as_windows.owner_checks), "the owner is read from the opened file"


def test_a_file_of_another_owner_is_refused_when_opened(tr, tmp_path, as_windows):
    path = make(tmp_path / f"{VID}.json")
    as_windows.mine = False
    with pytest.raises(tr.UnsafeStoreEntry, match="another user"):
        tr._open_regular(path)


def test_a_store_of_another_owner_stops_the_tool(tr, isolated_tmp, as_windows):
    isolated_tmp.mkdir()
    as_windows.mine = False
    with pytest.raises(SystemExit) as stopped:
        tr.ensure_store()
    assert stopped.value.code == tr.EXIT_STORAGE


def test_scanning_ignores_files_whose_owner_is_not_verified(tr, tmp_path, as_windows):
    make(tmp_path / f"{VID}.json", age_h=48)
    as_windows.mine = False
    assert tr._scan_store(tmp_path, None) == []


# ------------------------------------------------------------------------------ deletion by open handle (decisions)
def test_the_scanned_file_is_deleted_through_its_open_handle(tr, tmp_path, as_windows):
    path = make(tmp_path / f"{VID}.json", age_h=48)
    entry = scan_one(tr, tmp_path, path.name)
    assert tr._delete_exact(tmp_path, entry, time.time(), HOUR) is True
    assert len(as_windows.marked) == 1, "deletion is requested on the handle that was inspected"


def test_a_file_that_replaced_the_scanned_one_is_not_deleted(tr, tmp_path, as_windows):
    path = make(tmp_path / f"{VID}.json", age_h=48)
    entry = scan_one(tr, tmp_path, path.name)
    path.unlink()
    make(tmp_path / f"{VID}.json", age_h=48, text=DOC % OTHER)
    assert tr._delete_exact(tmp_path, entry, time.time(), HOUR) is False
    assert as_windows.marked == []


def test_a_refreshed_file_is_not_deleted(tr, tmp_path, as_windows):
    path = make(tmp_path / f"{VID}.json", age_h=48)
    entry = scan_one(tr, tmp_path, path.name)
    os.utime(path, None)
    assert tr._delete_exact(tmp_path, entry, time.time(), HOUR) is False
    assert as_windows.marked == []


def test_a_file_that_is_too_young_is_not_deleted(tr, tmp_path, as_windows):
    path = make(tmp_path / f"{VID}.json", age_h=48)
    entry = scan_one(tr, tmp_path, path.name)
    assert tr._delete_exact(tmp_path, entry, time.time(), 100 * HOUR) is False
    assert as_windows.marked == []


@pytest.mark.skipif(not hasattr(os, "link"), reason="needs hard links")
def test_a_file_with_a_second_name_is_not_deleted(tr, tmp_path, as_windows):
    path = make(tmp_path / f"{VID}.json", age_h=48)
    entry = scan_one(tr, tmp_path, path.name)
    os.link(path, tmp_path / "second-name.json")
    assert tr._delete_exact(tmp_path, entry, time.time(), HOUR) is False
    assert as_windows.marked == []


def test_a_link_or_junction_is_not_deleted(tr, tmp_path, as_windows):
    path = make(tmp_path / f"{VID}.json", age_h=48)
    entry = scan_one(tr, tmp_path, path.name)
    as_windows.reparse = True
    assert tr._delete_exact(tmp_path, entry, time.time(), HOUR) is False
    assert as_windows.marked == []


def test_a_file_whose_owner_changed_is_not_deleted(tr, tmp_path, as_windows):
    path = make(tmp_path / f"{VID}.json", age_h=48)
    entry = scan_one(tr, tmp_path, path.name)
    as_windows.mine = False
    assert tr._delete_exact(tmp_path, entry, time.time(), HOUR) is False
    assert as_windows.marked == []


def test_the_handle_is_closed_whatever_the_outcome(tr, tmp_path, as_windows, monkeypatch):
    path = make(tmp_path / f"{VID}.json", age_h=48)
    entry = scan_one(tr, tmp_path, path.name)
    opened = []
    real = tr._win_open_for_delete
    monkeypatch.setattr(tr, "_win_open_for_delete", lambda p: opened.append(real(p)) or opened[-1])
    as_windows.mine = False
    tr._delete_exact(tmp_path, entry, time.time(), HOUR)
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_a_failed_deletion_request_is_reported_and_the_file_stays(tr, tmp_path, as_windows, monkeypatch):
    path = make(tmp_path / f"{VID}.json", age_h=48)
    entry = scan_one(tr, tmp_path, path.name)

    def refuse(fd):
        raise PermissionError(5, "denied")

    monkeypatch.setattr(tr, "_win_mark_delete", refuse)
    assert tr._remove_if_unchanged(tmp_path, None, entry, time.time(), HOUR) is False
    assert path.exists()


@pytest.mark.parametrize("problem", [FileNotFoundError(2, "gone"), PermissionError(32, "sharing violation")])
def test_a_file_that_cannot_be_opened_for_deletion_is_kept(tr, tmp_path, as_windows, monkeypatch, problem):
    path = make(tmp_path / f"{VID}.json", age_h=48)
    entry = scan_one(tr, tmp_path, path.name)

    def cannot(p):
        raise problem

    monkeypatch.setattr(tr, "_win_open_for_delete", cannot)
    assert tr._remove_if_unchanged(tmp_path, None, entry, time.time(), HOUR) is False
    assert path.exists()


def test_a_stale_precheck_cannot_make_the_handle_check_delete_a_replacement(tr, tmp_path, as_windows, monkeypatch):
    """The path-based pre-check passes (it sees the old file), then the file is swapped before the delete step."""
    path = make(tmp_path / f"{VID}.json", age_h=48)
    entry = scan_one(tr, tmp_path, path.name)
    stale = os.lstat(path)
    path.unlink()
    make(tmp_path / f"{VID}.json", age_h=48, text=DOC % OTHER)
    monkeypatch.setattr(tr, "_stat_entry", lambda *a: stale)
    assert tr._remove_if_unchanged(tmp_path, None, entry, time.time(), HOUR) is False
    assert as_windows.marked == []
    assert (tmp_path / f"{VID}.json").read_text(encoding="utf-8") == DOC % OTHER


# ------------------------------------------------------------------------------ pinning the store folder
def test_nothing_is_pinned_on_posix(tr, tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX only")
    assert tr._pin_store(tmp_path) is None


def test_the_pin_is_refused_for_a_folder_that_is_not_mine(tr, tmp_path, as_windows):
    as_windows.mine = False
    with pytest.raises(tr.UnsafeStoreEntry, match="another user"):
        tr._pin_store(tmp_path)


def test_the_pin_is_refused_for_a_link_or_junction(tr, tmp_path, as_windows):
    as_windows.reparse = True
    with pytest.raises(tr.UnsafeStoreEntry, match="not a plain folder"):
        tr._pin_store(tmp_path)


def test_the_pin_is_refused_for_something_that_is_not_a_folder(tr, tmp_path, as_windows, monkeypatch):
    path = make(tmp_path / f"{VID}.json")
    monkeypatch.setattr(tr, "_win_pin_dir", lambda p: os.open(path, os.O_RDONLY))
    with pytest.raises(tr.UnsafeStoreEntry, match="not a plain folder"):
        tr._pin_store(tmp_path)


def test_the_pin_is_closed_when_it_is_refused(tr, tmp_path, as_windows, monkeypatch):
    opened = []
    real = tr._win_pin_dir
    monkeypatch.setattr(tr, "_win_pin_dir", lambda p: opened.append(real(p)) or opened[-1])
    as_windows.mine = False
    with pytest.raises(tr.UnsafeStoreEntry):
        tr._pin_store(tmp_path)
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_cleanup_on_windows_deletes_only_what_is_due_and_closes_its_pin(tr, isolated_tmp, as_windows, monkeypatch):
    isolated_tmp.mkdir()
    old = make(isolated_tmp / f"{VID}.json", age_h=24 * 30)
    fresh = make(isolated_tmp / f"{OTHER}.json", age_h=0)
    pins = []
    real = tr._win_pin_dir
    monkeypatch.setattr(tr, "_win_pin_dir", lambda p: pins.append(real(p)) or pins[-1])
    deleted = []
    monkeypatch.setattr(tr, "_win_mark_delete", lambda fd: deleted.append(os.fstat(fd).st_ino))
    result = tr.cleanup_store(isolated_tmp)
    assert result["expired"] == 1 and deleted == [os.stat(old).st_ino]
    assert fresh.exists()
    with pytest.raises(OSError):
        os.fstat(pins[0])


def test_cleanup_on_windows_deletes_nothing_if_the_folder_cannot_be_pinned(tr, isolated_tmp, as_windows, monkeypatch):
    isolated_tmp.mkdir()
    make(isolated_tmp / f"{VID}.json", age_h=24 * 30)

    def cannot(path):
        raise PermissionError(5, "denied")

    monkeypatch.setattr(tr, "_win_pin_dir", cannot)
    result = tr.cleanup_store(isolated_tmp)
    assert result["expired"] == 0 and (isolated_tmp / f"{VID}.json").exists()


def test_cleanup_on_windows_deletes_nothing_in_a_folder_of_another_owner(tr, isolated_tmp, as_windows):
    isolated_tmp.mkdir()
    make(isolated_tmp / f"{VID}.json", age_h=24 * 30)
    as_windows.mine = False
    result = tr.cleanup_store(isolated_tmp)
    assert result["expired"] == 0 and (isolated_tmp / f"{VID}.json").exists()


# ------------------------------------------------------------------------------ real Windows behaviour
@pytest.fixture
def win_dir(tmp_path):
    folder = tmp_path / "store"
    folder.mkdir()
    return folder


@on_windows
def test_real_windows_reports_the_current_user_as_owner_of_new_files_and_folders(tr, win_dir):
    path = make(win_dir / f"{VID}.json")
    assert tr._windows_owned_by_me(path) is True
    assert tr._windows_owned_by_me(win_dir) is True
    fd = os.open(path, os.O_RDONLY | os.O_BINARY)
    try:
        assert tr._windows_owned_by_me(fd) is True
        assert tr._is_mine(os.fstat(fd), fd) is True
    finally:
        os.close(fd)


@on_windows
def test_real_windows_does_not_trust_a_missing_path(tr, win_dir):
    assert tr._windows_owned_by_me(win_dir / "missing.json") is False


@on_windows
def test_real_windows_refuses_a_file_owned_by_someone_else(tr, win_dir):
    path = make(win_dir / f"{VID}.json")
    changed = subprocess.run([ICACLS, str(path), "/setowner", "*S-1-5-18"], capture_output=True, text=True)
    if changed.returncode != 0:
        pytest.skip(f"this account may not hand a file to another owner: {changed.stdout.strip()[:120]}")
    assert tr._windows_owned_by_me(path) is False
    with pytest.raises(tr.UnsafeStoreEntry, match="another user"):
        tr._open_regular(path)


@on_windows
def test_real_windows_refuses_a_store_owned_by_someone_else(tr, win_dir, monkeypatch):
    changed = subprocess.run([ICACLS, str(win_dir), "/setowner", "*S-1-5-18"], capture_output=True, text=True)
    if changed.returncode != 0:
        pytest.skip(f"this account may not hand a folder to another owner: {changed.stdout.strip()[:120]}")
    monkeypatch.setattr(tr, "default_dir", lambda: win_dir)
    with pytest.raises(SystemExit) as stopped:
        tr.ensure_store()
    assert stopped.value.code == tr.EXIT_STORAGE
    with pytest.raises(tr.UnsafeStoreEntry):
        tr._pin_store(win_dir)


@on_windows
def test_real_windows_deletes_the_scanned_file_through_its_handle(tr, win_dir):
    path = make(win_dir / f"{VID}.json", age_h=48)
    entry = scan_one(tr, win_dir, path.name)
    assert tr._remove_if_unchanged(win_dir, None, entry, time.time(), HOUR) is True
    assert not path.exists()


@on_windows
def test_real_windows_never_deletes_a_file_that_replaced_the_scanned_one(tr, win_dir, monkeypatch):
    path = make(win_dir / f"{VID}.json", age_h=48)
    entry = scan_one(tr, win_dir, path.name)
    stale = os.lstat(path)
    path.unlink()
    make(win_dir / f"{VID}.json", age_h=48, text=DOC % OTHER)
    monkeypatch.setattr(tr, "_stat_entry", lambda *a: stale)    # the path-based check is fooled; the handle check is not
    assert tr._remove_if_unchanged(win_dir, None, entry, time.time(), HOUR) is False
    assert (win_dir / f"{VID}.json").read_text(encoding="utf-8") == DOC % OTHER


@on_windows
def test_real_windows_never_deletes_a_file_with_a_second_name(tr, win_dir):
    path = make(win_dir / f"{VID}.json", age_h=48)
    entry = scan_one(tr, win_dir, path.name)
    os.link(path, win_dir / "second-name.json")
    assert tr._delete_exact(win_dir, entry, time.time(), HOUR) is False
    assert path.exists() and (win_dir / "second-name.json").exists()


@on_windows
def test_real_windows_never_follows_a_symbolic_link_into_deletion(tr, win_dir, tmp_path):
    path = make(win_dir / f"{VID}.json", age_h=48)
    entry = scan_one(tr, win_dir, path.name)
    victim = make(tmp_path / "victim.json", age_h=48)
    path.unlink()
    try:
        os.symlink(victim, path)
    except OSError as exc:
        pytest.skip(f"this account may not create symbolic links: {exc}")
    assert tr._remove_if_unchanged(win_dir, None, entry, time.time(), HOUR) is False
    assert victim.exists()


@on_windows
def test_real_windows_keeps_a_file_that_another_program_holds_open(tr, win_dir):
    path = make(win_dir / f"{VID}.json", age_h=48)
    entry = scan_one(tr, win_dir, path.name)
    with open(path, "rb"):          # Python opens without FILE_SHARE_DELETE: Windows must refuse the delete
        assert tr._remove_if_unchanged(win_dir, None, entry, time.time(), HOUR) is False
    assert path.exists()


@on_windows
def test_real_windows_pinned_store_cannot_be_renamed_or_replaced(tr, win_dir):
    fd = tr._pin_store(win_dir)
    try:
        with pytest.raises(OSError):
            os.rename(win_dir, win_dir.with_name("moved"))
        assert win_dir.is_dir()
    finally:
        os.close(fd)
    os.rename(win_dir, win_dir.with_name("moved"))      # released once cleanup is finished


@on_windows
def test_real_windows_junction_as_store_is_refused(tr, tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    junction = tmp_path / "junction"
    made = subprocess.run([CMD, "/c", "mklink", "/J", str(junction), str(target)], capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip(f"could not create a junction: {made.stdout.strip()[:120]}")
    info = os.lstat(junction)
    assert not stat.S_ISLNK(info.st_mode), "a junction is not a symbolic link to lstat; that is why it needs its own check"
    with pytest.raises(tr.UnsafeStoreEntry, match="not a plain folder"):
        tr._pin_store(junction)


@on_windows
def test_real_windows_cleanup_end_to_end(tr, isolated_tmp):
    isolated_tmp.mkdir()
    old = make(isolated_tmp / f"{VID}.json", age_h=24 * 30)
    fresh = make(isolated_tmp / f"{OTHER}.json")
    result = tr.cleanup_store(isolated_tmp)
    assert result["expired"] == 1 and not old.exists() and fresh.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX behaviour")
def test_cleanup_deletes_nothing_where_a_safe_delete_is_not_possible(tr, isolated_tmp, monkeypatch):
    isolated_tmp.mkdir()
    old = make(isolated_tmp / f"{VID}.json", age_h=24 * 30)
    monkeypatch.setattr(tr, "_HAS_DIR_FD", False)          # a system without directory descriptors
    messages = []
    import logging

    class Collect(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    handler = Collect()
    tr.log.addHandler(handler)
    try:
        result = tr.cleanup_store(isolated_tmp)
    finally:
        tr.log.removeHandler(handler)
    assert result["expired"] == 0 and old.exists()
    assert any("cannot delete files in the store safely" in m for m in messages)
