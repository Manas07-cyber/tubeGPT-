"""Storage security: permissions, symbolic links, ownership, the cleanup race, custom folders and concurrency.

Tests that need POSIX permissions, symbolic links or user IDs are marked ``posix_only`` and skipped on Windows
with that reason; everything else runs everywhere.
"""
import errno
import json
import os
import stat
import subprocess
import sys
import time

import pytest

from conftest import SCRIPT

VID = "dQw4w9WgXcQ"
OTHER = "aaaaaaaaaaa"
HOUR = 3600
DOC = {"video_id": VID, "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}], "has_timestamps": True}
posix_only = pytest.mark.skipif(os.name != "posix", reason="needs POSIX permissions, symlinks and user IDs")


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def write_doc(path, doc=DOC, age_h=0.0, perm=None):
    path.write_text(json.dumps(doc), encoding="utf-8")
    t = time.time() - age_h * HOUR
    os.utime(path, (t, t))
    if perm is not None:
        os.chmod(path, perm)
    return path


# ------------------------------------------------------------------------- A. permissions
@posix_only
def test_existing_755_default_store_and_644_transcripts_are_tightened(tr, isolated_tmp):
    isolated_tmp.mkdir()
    isolated_tmp.chmod(0o755)
    mine = write_doc(isolated_tmp / f"{VID}.json", perm=0o644)
    pasted = write_doc(isolated_tmp / f"pasted-{'a' * 32}.json", perm=0o666)
    legacy = write_doc(isolated_tmp / "pasted-deadbeef.json", perm=0o644)
    leftover = isolated_tmp / ".tmp-abcd1234.json"
    leftover.write_text("{}")
    leftover.chmod(0o644)
    tr.ensure_store()
    assert mode(isolated_tmp) == 0o700
    for path in (mine, pasted, legacy, leftover):
        assert mode(path) == 0o600, path.name


@posix_only
def test_unrelated_files_in_the_default_store_are_never_modified(tr, isolated_tmp):
    isolated_tmp.mkdir()
    keep = isolated_tmp / "notes.txt"
    keep.write_text("mine")
    keep.chmod(0o644)
    sub = isolated_tmp / "folder"
    sub.mkdir()
    sub.chmod(0o755)
    before = (keep.read_text(), mode(keep), mode(sub), os.lstat(keep).st_mtime_ns)
    tr.ensure_store()
    assert (keep.read_text(), mode(keep), mode(sub), os.lstat(keep).st_mtime_ns) == before


@posix_only
def test_a_custom_folder_without_our_marker_is_never_re_permissioned(tr, tmp_path, monkeypatch):
    mine = tmp_path / "documents"
    mine.mkdir()
    mine.chmod(0o755)
    other = write_doc(mine / f"{VID}.json", perm=0o644)   # looks like ours, but the folder is not
    (mine / "x.txt").write_text("user")
    monkeypatch.setenv("TUBEGPT_STORAGE_DIR", str(mine))
    monkeypatch.setattr(tr, "default_dir", lambda: mine)
    tr.ensure_store()
    assert mode(mine) == 0o755 and mode(other) == 0o644
    assert not (mine / tr.STORE_MARKER).exists()          # a non-empty unrelated folder is not adopted


@posix_only
def test_a_marked_custom_folder_is_tightened_but_only_our_files(tr, tmp_path, monkeypatch):
    mine = tmp_path / "tube"
    monkeypatch.setenv("TUBEGPT_STORAGE_DIR", str(mine))
    monkeypatch.setattr(tr, "default_dir", lambda: mine)
    tr.ensure_store()                                      # we create it: marker written
    assert (mine / tr.STORE_MARKER).is_file()
    mine.chmod(0o755)
    ours = write_doc(mine / f"{VID}.json", perm=0o644)
    user_file = mine / "report.json"
    user_file.write_text("{}")
    user_file.chmod(0o644)
    tr.ensure_store()
    assert mode(mine) == 0o700 and mode(ours) == 0o600 and mode(user_file) == 0o644


@posix_only
def test_a_symlink_named_like_a_transcript_is_not_chmodded(tr, isolated_tmp, tmp_path):
    isolated_tmp.mkdir()
    target = tmp_path / "victim.txt"
    target.write_text("private")
    target.chmod(0o644)
    (isolated_tmp / f"{VID}.json").symlink_to(target)
    tr.ensure_store()
    assert mode(target) == 0o644                           # the link target was left alone


@posix_only
def test_files_of_another_user_are_not_modified(tr, isolated_tmp, monkeypatch):
    isolated_tmp.mkdir()
    foreign = write_doc(isolated_tmp / f"{VID}.json", perm=0o644)
    monkeypatch.setattr(tr, "_owned_by_me", lambda info: not stat.S_ISREG(info.st_mode))   # files look foreign
    assert tr.secure_store_files(isolated_tmp) == 0
    assert mode(foreign) == 0o644


@posix_only
def test_a_store_folder_owned_by_someone_else_is_refused(run, tr, isolated_tmp, monkeypatch, tmp_path):
    isolated_tmp.mkdir()
    monkeypatch.setattr(tr, "_owned_by_me", lambda info: False)
    f = tmp_path / "n.txt"
    f.write_text("a line of notes", encoding="utf-8")
    code, _, err = run("parse", str(f))
    assert code == tr.EXIT_STORAGE and "another user" in err and "Traceback" not in err
    assert list(isolated_tmp.iterdir()) == []


@posix_only
def test_permission_failures_while_tightening_are_controlled_errors(run, tr, isolated_tmp, monkeypatch, tmp_path):
    isolated_tmp.mkdir()
    isolated_tmp.chmod(0o755)

    def refuse(*args, **kwargs):
        raise PermissionError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(os, "fchmod", refuse)
    f = tmp_path / "n.txt"
    f.write_text("a line of notes", encoding="utf-8")
    code, _, err = run("parse", str(f))
    assert code == tr.EXIT_STORAGE and "Permission was denied" in err
    assert "Traceback" not in err and str(isolated_tmp) not in err


@posix_only
def test_a_new_store_and_its_transcripts_are_private(run, tr, isolated_tmp, tmp_path):
    f = tmp_path / "n.txt"
    f.write_text("a line of notes", encoding="utf-8")
    assert run("parse", str(f), "--video", VID)[0] == 0
    assert mode(isolated_tmp) == 0o700
    assert mode(isolated_tmp / f"{VID}.json") == 0o600


# ------------------------------------------------------------------------- B. cached reads
@posix_only
def test_cached_read_refuses_a_symlinked_store_folder(run, tr, tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    write_doc(real / f"{VID}.json")
    link = tmp_path / "store-link"
    link.symlink_to(real)
    monkeypatch.setattr(tr, "default_dir", lambda: link)
    for argv in (("show", VID), ("search", VID, "hello"), ("link", VID, "0:01"), ("show", str(link / f"{VID}.json"))):
        code, out, err = run(*argv)
        assert code == tr.EXIT_STORAGE and "symbolic link" in err and "hello" not in out, argv


@posix_only
def test_cached_read_refuses_a_symlinked_transcript_file(run, tr, isolated_tmp, tmp_path):
    isolated_tmp.mkdir()
    secret = write_doc(tmp_path / "elsewhere.json", {**DOC, "segments": [{"start": 0.0, "text": "TOPSECRET"}]})
    (isolated_tmp / f"{VID}.json").symlink_to(secret)
    for argv in (("show", VID), ("search", VID, "x"), ("link", VID, "0:01"),
                 ("show", str(isolated_tmp / f"{VID}.json"))):
        code, out, err = run(*argv)
        assert code == tr.EXIT_STORAGE and "never followed" in err and "TOPSECRET" not in out, argv


@posix_only
def test_a_symlink_to_a_folder_or_a_fifo_inside_the_store_is_not_read(run, tr, isolated_tmp, tmp_path):
    isolated_tmp.mkdir()
    (isolated_tmp / f"{VID}.json").symlink_to(tmp_path)
    os.mkfifo(isolated_tmp / f"{OTHER}.json")
    assert run("show", VID)[0] == tr.EXIT_STORAGE
    code, _, err = run("show", OTHER)                      # must not block on the pipe
    assert code == tr.EXIT_STORAGE and "regular" in err


@posix_only
def test_a_foreign_file_in_the_store_is_not_read(run, tr, isolated_tmp, monkeypatch):
    isolated_tmp.mkdir()
    write_doc(isolated_tmp / f"{VID}.json")
    monkeypatch.setattr(tr, "_owned_by_me", lambda info: not stat.S_ISREG(info.st_mode))
    code, out, err = run("show", VID)
    assert code == tr.EXIT_STORAGE and "owned by you" in err and "hello" not in out


@posix_only
def test_reading_tightens_a_644_transcript_and_counts_as_use(run, tr, isolated_tmp):
    isolated_tmp.mkdir()
    path = write_doc(isolated_tmp / f"{VID}.json", age_h=10, perm=0o644)
    before = os.lstat(path).st_mtime
    assert run("show", VID)[0] == 0
    assert mode(path) == 0o600 and os.lstat(path).st_mtime > before + 9 * HOUR


@posix_only
def test_an_explicit_path_outside_the_store_that_is_a_link_is_read_as_asked(run, tr, tmp_path):
    real = write_doc(tmp_path / "mine.json")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    code, out, _ = run("show", str(link))
    assert code == 0 and "hello" in out                    # the user named it; only the store refuses links


def test_the_store_lock_file_is_not_reported_as_a_transcript_and_survives_cleanup(tr, isolated_tmp):
    store = tr.ensure_store()
    with tr.store_lock(store) as held:
        assert held in (True, False)
    result = tr.cleanup_store(store)
    assert result["remaining"] == 0
    assert (store / tr.LOCK_NAME).exists() or os.name != "posix"


# ------------------------------------------------------------------------- C. cleanup races
def test_a_file_replaced_after_the_scan_is_not_deleted(tr, isolated_tmp, monkeypatch):
    isolated_tmp.mkdir()
    write_doc(isolated_tmp / f"{VID}.json", age_h=48)
    original = tr._remove_if_unchanged

    def replace_then_remove(store, dirfd, entry, now, min_age):
        fresh = isolated_tmp / ".swap.json"                # another process saved a new copy of the same video
        fresh.write_text(json.dumps({**DOC, "segments": [{"start": 0.0, "text": "NEW COPY"}]}), encoding="utf-8")
        os.replace(fresh, isolated_tmp / f"{VID}.json")
        old = time.time() - 48 * HOUR                      # even with an identical old timestamp, the inode differs
        os.utime(isolated_tmp / f"{VID}.json", (old, old))
        return original(store, dirfd, entry, now, min_age)
    monkeypatch.setattr(tr, "_remove_if_unchanged", replace_then_remove)
    result = tr.cleanup_store(isolated_tmp)
    assert result["expired"] == 0
    assert "NEW COPY" in (isolated_tmp / f"{VID}.json").read_text(encoding="utf-8")


def test_a_file_used_after_the_scan_is_not_deleted(tr, isolated_tmp, monkeypatch):
    isolated_tmp.mkdir()
    path = write_doc(isolated_tmp / f"{VID}.json", age_h=48)
    original = tr._remove_if_unchanged

    def touch_then_remove(store, dirfd, entry, now, min_age):
        os.utime(path)                                     # a reader just used it
        return original(store, dirfd, entry, now, min_age)
    monkeypatch.setattr(tr, "_remove_if_unchanged", touch_then_remove)
    assert tr.cleanup_store(isolated_tmp)["expired"] == 0 and path.exists()


def test_a_file_rewritten_in_place_after_the_scan_is_not_deleted(tr, isolated_tmp, monkeypatch):
    isolated_tmp.mkdir()
    path = write_doc(isolated_tmp / f"{VID}.json", age_h=48)
    original = tr._remove_if_unchanged
    stamp = os.lstat(path)

    def rewrite_then_remove(store, dirfd, entry, now, min_age):
        path.write_text(json.dumps({**DOC, "segments": [{"start": 0.0, "text": "much longer text now"}]}))
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))   # same mtime, different size
        return original(store, dirfd, entry, now, min_age)
    monkeypatch.setattr(tr, "_remove_if_unchanged", rewrite_then_remove)
    assert tr.cleanup_store(isolated_tmp)["expired"] == 0 and path.exists()


@posix_only
def test_a_file_swapped_for_a_symlink_after_the_scan_is_not_followed_or_deleted(tr, isolated_tmp, tmp_path, monkeypatch):
    isolated_tmp.mkdir()
    write_doc(isolated_tmp / f"{VID}.json", age_h=48)
    victim = write_doc(tmp_path / "victim.json", age_h=48)
    original = tr._remove_if_unchanged

    def swap_then_remove(store, dirfd, entry, now, min_age):
        os.unlink(isolated_tmp / f"{VID}.json")
        (isolated_tmp / f"{VID}.json").symlink_to(victim)
        return original(store, dirfd, entry, now, min_age)
    monkeypatch.setattr(tr, "_remove_if_unchanged", swap_then_remove)
    tr.cleanup_store(isolated_tmp)
    assert victim.exists()                                 # the target of the link was never touched


@posix_only
def test_cleanup_never_deletes_symlinks_or_what_they_point_to(tr, isolated_tmp, tmp_path):
    isolated_tmp.mkdir()
    victim = write_doc(tmp_path / "victim.json", age_h=500)
    link = isolated_tmp / f"{VID}.json"
    link.symlink_to(victim)
    tr.cleanup_store(isolated_tmp)
    assert victim.exists() and link.is_symlink()


def test_a_failed_removal_is_reported_and_does_not_stop_the_rest(tr, isolated_tmp, monkeypatch, caplog):
    isolated_tmp.mkdir()
    for n in range(3):
        write_doc(isolated_tmp / f"{n:011d}.json", age_h=48)
    real_unlink = os.unlink

    def flaky(path, *args, **kwargs):
        if str(path).endswith("00000000001.json"):
            raise PermissionError(errno.EACCES, "denied")
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(os, "unlink", flaky)
    real_open = tr._win_open_for_delete if os.name == "nt" else None

    def flaky_open(path):                      # Windows deletes through an open handle, not os.unlink
        if str(path).endswith("00000000001.json"):
            raise PermissionError(errno.EACCES, "denied")
        return real_open(path)
    if os.name == "nt":
        monkeypatch.setattr(tr, "_win_open_for_delete", flaky_open)
    result = tr.cleanup_store(isolated_tmp)
    assert result["expired"] == 2                          # the two removable files went, the stuck one stays
    assert sorted(p.name for p in isolated_tmp.iterdir() if p.name != tr.LOCK_NAME) == ["00000000001.json"]


def test_a_failed_save_after_cleanup_leaves_live_files_and_no_temp_files(run, tr, isolated_tmp, monkeypatch, tmp_path):
    isolated_tmp.mkdir()
    write_doc(isolated_tmp / f"{OTHER}.json", age_h=1)
    write_doc(isolated_tmp / f"{'b' * 11}.json", age_h=48)       # expired: removed by the cleanup before the save
    f = tmp_path / "n.txt"
    f.write_text("a line of notes", encoding="utf-8")

    def disk_full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(tr.tempfile, "mkstemp", disk_full)
    code, _, err = run("parse", str(f), "--video", VID)
    assert code == tr.EXIT_STORAGE and "free disk space" in err and "Traceback" not in err
    left = sorted(p.name for p in isolated_tmp.iterdir() if p.name != tr.LOCK_NAME)
    assert left == [f"{OTHER}.json"]                      # live file intact, expired one gone, no temp files


def test_a_failure_while_writing_removes_the_temporary_file(tr, isolated_tmp, monkeypatch):
    isolated_tmp.mkdir()
    real_replace = os.replace

    def boom(src, dst):
        raise OSError(errno.EIO, "I/O error")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(SystemExit) as info:
        tr.write_json_atomic(isolated_tmp / f"{VID}.json", DOC)
    monkeypatch.setattr(os, "replace", real_replace)
    assert info.value.code == tr.EXIT_STORAGE
    assert list(isolated_tmp.iterdir()) == []


# ------------------------------------------------------------------------- D. custom storage
def test_the_marker_alone_never_enables_deletion(tr, tmp_path, monkeypatch):
    mine = tmp_path / "tube"
    monkeypatch.setenv("TUBEGPT_STORAGE_DIR", str(mine))
    monkeypatch.setattr(tr, "default_dir", lambda: mine)
    tr.ensure_store()
    assert tr.marker_is_valid(mine)
    old = write_doc(mine / f"{VID}.json", age_h=500)
    assert tr.cleanup_store(mine)["expired"] == 0 and old.exists()          # no opt-in: nothing deleted
    monkeypatch.setenv("TUBEGPT_STORAGE_CLEANUP", "yes")                    # only the exact value 1 counts
    assert tr.cleanup_store(mine)["expired"] == 0 and old.exists()
    monkeypatch.setenv("TUBEGPT_STORAGE_CLEANUP", "1")
    assert tr.cleanup_store(mine)["expired"] == 1 and not old.exists()


def test_opt_in_without_a_marker_deletes_nothing(tr, tmp_path, monkeypatch):
    mine = tmp_path / "user-folder"
    mine.mkdir()
    old = write_doc(mine / f"{VID}.json", age_h=500)
    monkeypatch.setenv("TUBEGPT_STORAGE_DIR", str(mine))
    monkeypatch.setenv("TUBEGPT_STORAGE_CLEANUP", "1")
    monkeypatch.setattr(tr, "default_dir", lambda: mine)
    assert tr.cleanup_store(mine)["expired"] == 0 and old.exists()


def test_an_enabled_custom_store_only_removes_files_with_our_names(tr, tmp_path, monkeypatch):
    mine = tmp_path / "tube"
    monkeypatch.setenv("TUBEGPT_STORAGE_DIR", str(mine))
    monkeypatch.setenv("TUBEGPT_STORAGE_CLEANUP", "1")
    monkeypatch.setattr(tr, "default_dir", lambda: mine)
    tr.ensure_store()
    ours = write_doc(mine / f"{VID}.json", age_h=500)
    for name in ("thesis.json", "photo.jpg", "notes.txt", "backup.json.bak", f"{VID}.json.old", "short.json"):
        (mine / name).write_text("user data")
        os.utime(mine / name, (1, 1))
    (mine / "sub").mkdir()
    (mine / "sub" / f"{OTHER}.json").write_text("nested user data")
    tr.cleanup_store(mine)
    assert not ours.exists()
    assert (mine / "thesis.json").exists() and (mine / "photo.jpg").exists() and (mine / "notes.txt").exists()
    assert (mine / "backup.json.bak").exists() and (mine / f"{VID}.json.old").exists()
    assert (mine / "short.json").exists() and (mine / "sub" / f"{OTHER}.json").exists()


@posix_only
def test_a_symlinked_custom_folder_is_refused(run, tr, tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("TUBEGPT_STORAGE_DIR", str(link))
    monkeypatch.setattr(tr, "default_dir", lambda: link)
    f = tmp_path / "n.txt"
    f.write_text("a line of notes", encoding="utf-8")
    code, _, err = run("parse", str(f))
    assert code == tr.EXIT_STORAGE and "symbolic link" in err
    assert list(real.iterdir()) == []


@posix_only
def test_a_symlinked_marker_is_not_a_marker(tr, tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    genuine = tmp_path / "genuine-marker"
    genuine.write_text(tr.MARKER_TEXT)
    (folder / tr.STORE_MARKER).symlink_to(genuine)
    assert not tr.marker_is_valid(folder)


def test_a_marker_with_foreign_content_or_huge_size_is_not_a_marker(tr, tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    (folder / tr.STORE_MARKER).write_text("hello")
    assert not tr.marker_is_valid(folder)
    (folder / tr.STORE_MARKER).write_text(tr.MARKER_PREFIX + "x" * 10000)
    assert not tr.marker_is_valid(folder)
    assert not tr.marker_is_valid(tmp_path / "does-not-exist")


def test_marker_creation_never_overwrites_an_existing_file(tr, tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    (folder / tr.STORE_MARKER).write_text("someone else's file")
    tr._write_marker(folder)                               # O_EXCL: loses the race quietly
    assert (folder / tr.STORE_MARKER).read_text() == "someone else's file"


def test_a_non_empty_unrelated_custom_folder_is_not_adopted(tr, tmp_path, monkeypatch):
    mine = tmp_path / "documents"
    mine.mkdir()
    (mine / "thesis.docx").write_text("x")
    monkeypatch.setenv("TUBEGPT_STORAGE_DIR", str(mine))
    monkeypatch.setattr(tr, "default_dir", lambda: mine)
    tr.ensure_store()
    assert not (mine / tr.STORE_MARKER).exists() and not (mine / tr.LOCK_NAME).exists()


def test_saving_into_an_unmarked_custom_folder_creates_no_lock_file_and_never_deletes(run, tr, tmp_path, monkeypatch):
    mine = tmp_path / "documents"
    mine.mkdir()
    old = write_doc(mine / f"{OTHER}.json", age_h=900)
    monkeypatch.setenv("TUBEGPT_STORAGE_DIR", str(mine))
    monkeypatch.setenv("TUBEGPT_STORAGE_CLEANUP", "1")
    monkeypatch.setattr(tr, "default_dir", lambda: mine)
    f = tmp_path / "n.txt"
    f.write_text("a line of notes", encoding="utf-8")
    assert run("parse", str(f), "--video", VID)[0] == 0
    assert old.exists() and not (mine / tr.LOCK_NAME).exists()


def test_explicit_out_files_are_never_touched_by_cleanup(run, tr, isolated_tmp, tmp_path):
    out = write_doc(tmp_path / "mine.json", age_h=900)
    isolated_tmp.mkdir()
    write_doc(isolated_tmp / f"{OTHER}.json", age_h=900)
    tr.cleanup_store(isolated_tmp)
    assert out.exists()


# ------------------------------------------------------------------------- concurrency (real processes)
def cli(*argv, env):
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *argv], env=env, capture_output=True, text=True,
                          timeout=120, check=False)


@pytest.fixture
def shared_store(tmp_path, tr):
    store = tmp_path / "shared"
    store.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("TUBEGPT_")}
    env.update(TUBEGPT_STORAGE_DIR=str(store), TUBEGPT_STORAGE_CLEANUP="1", TUBEGPT_RETENTION_HOURS="24")
    # create the marker through the program itself, exactly as a first run would
    assert cli("cleanup", env=env).returncode == 0
    probe = tmp_path / "probe.txt"
    probe.write_text("probe line", encoding="utf-8")
    assert cli("parse", str(probe), "--video", "zzzzzzzzzzz", env=env).returncode == 0
    (store / "zzzzzzzzzzz.json").unlink()
    return store, env


def spawn(*argv, env):
    return subprocess.Popen([sys.executable, "-I", str(SCRIPT), *argv], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def test_concurrent_saves_of_the_same_video_leave_one_valid_file(shared_store, tmp_path):
    store, env = shared_store
    sources = []
    for n in range(8):
        src = tmp_path / f"in{n}.txt"
        src.write_text(f"0:00 version {n} of the transcript\n0:05 second line {n}\n", encoding="utf-8")
        sources.append(src)
    procs = [spawn("parse", str(s), "--video", VID, env=env) for s in sources]
    results = [(p.wait(timeout=120), p.stderr.read()) for p in procs]
    assert all(code == 0 for code, _ in results), results
    names = sorted(p.name for p in store.iterdir() if p.name not in (".tubegpt-lock", ".tubegpt-store"))
    assert names == [f"{VID}.json"]                       # no stray temp files
    doc = json.loads((store / f"{VID}.json").read_text(encoding="utf-8"))
    assert doc["segments"][0]["text"].startswith("version ") and len(doc["segments"]) == 2   # one complete version
    assert cli("show", VID, env=env).returncode == 0


def test_concurrent_cleanup_and_save_never_lose_a_fresh_transcript(shared_store, tmp_path):
    store, env = shared_store
    for n in range(40):
        write_doc(store / f"{n:011d}.json", age_h=48)
    src = tmp_path / "in.txt"
    src.write_text("0:00 fresh line\n0:05 another\n", encoding="utf-8")
    procs = []
    for n in range(6):
        procs.append(spawn("cleanup", env=env))
        procs.append(spawn("parse", str(src), "--video", f"fresh{n}aaaaa"[:11], env=env))
    results = [(p.wait(timeout=120), p.stderr.read()) for p in procs]
    assert all(code == 0 for code, _ in results), results
    left = sorted(p.name for p in store.iterdir() if p.name not in (".tubegpt-lock", ".tubegpt-store"))
    assert len(left) == 6 and all(name.startswith("fresh") for name in left)      # every old file gone, every new one kept
    for name in left:
        json.loads((store / name).read_text(encoding="utf-8"))


def test_several_cleanup_processes_at_once_agree_and_do_not_error(shared_store):
    store, env = shared_store
    for n in range(60):
        write_doc(store / f"{n:011d}.json", age_h=48)
    keep = write_doc(store / f"{'k' * 11}.json", age_h=1)
    procs = [spawn("cleanup", env=env) for _ in range(8)]
    results = [(p.wait(timeout=120), p.stderr.read()) for p in procs]
    assert all(code == 0 and "Traceback" not in err for code, err in results), results
    assert [p.name for p in store.iterdir() if p.name not in (".tubegpt-lock", ".tubegpt-store")] == [keep.name]


def test_a_reader_and_cleanup_at_once_never_see_a_half_deleted_file(shared_store):
    store, env = shared_store
    write_doc(store / f"{VID}.json", age_h=23.9)           # about to expire: reading it must keep it alive
    procs = [spawn("show", VID, env=env) for _ in range(4)] + [spawn("cleanup", env=env) for _ in range(4)]
    results = [(p.wait(timeout=120), p.stdout.read(), p.stderr.read()) for p in procs]
    assert all(code == 0 for code, _, _ in results), results
    assert (store / f"{VID}.json").exists()
