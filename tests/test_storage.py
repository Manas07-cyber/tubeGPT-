"""Storage behaviour: retention, limits, safe deletion, atomic writes, path safety and clear errors."""
import errno
import os
import time

import pytest

VID = "dQw4w9WgXcQ"
HOUR = 3600
DOC = {"video_id": VID, "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}], "has_timestamps": True}
posix_only = pytest.mark.skipif(os.name != "posix", reason="needs POSIX file permissions and symlinks")


def make_store(tr, store, files, now=None):
    """Create transcript-like files in the store: files = {name: (age_hours, size_bytes)}."""
    store.mkdir(parents=True, exist_ok=True)
    now = now or time.time()
    for name, (age_h, size) in files.items():
        path = store / name
        path.write_bytes(b"x" * size)
        os.utime(path, (now - age_h * HOUR, now - age_h * HOUR))
    return now


def names(store):
    return sorted(p.name for p in store.iterdir())


def vid(n):
    return f"{n:011d}"  # 11 characters from [A-Za-z0-9_-]


# ---------------------------------------------------------------- retention
def test_expired_files_are_removed_and_fresh_kept(tr, isolated_tmp):
    make_store(tr, isolated_tmp, {f"{vid(1)}.json": (25, 10), f"{vid(2)}.json": (23, 10)})
    result = tr.cleanup_store(isolated_tmp)
    assert names(isolated_tmp) == [f"{vid(2)}.json"]
    assert result["expired"] == 1 and result["remaining"] == 1


def test_retention_is_configurable(tr, isolated_tmp, monkeypatch):
    monkeypatch.setenv("TUBEGPT_RETENTION_HOURS", "1")
    make_store(tr, isolated_tmp, {f"{vid(1)}.json": (2, 10), f"{vid(2)}.json": (0.5, 10)})
    tr.cleanup_store(isolated_tmp)
    assert names(isolated_tmp) == [f"{vid(2)}.json"]


def test_long_retention_keeps_old_files(tr, isolated_tmp, monkeypatch):
    monkeypatch.setenv("TUBEGPT_RETENTION_HOURS", "720")
    make_store(tr, isolated_tmp, {f"{vid(1)}.json": (100, 10)})
    tr.cleanup_store(isolated_tmp)
    assert names(isolated_tmp) == [f"{vid(1)}.json"]


def test_pasted_files_are_covered_too(tr, isolated_tmp):
    make_store(tr, isolated_tmp, {"pasted-0123abcd.json": (30, 10)})
    tr.cleanup_store(isolated_tmp)
    assert names(isolated_tmp) == []


# ---------------------------------------------------------------- limits
def test_file_count_limit_removes_oldest_first(tr, isolated_tmp, monkeypatch):
    monkeypatch.setenv("TUBEGPT_MAX_FILES", "3")
    make_store(tr, isolated_tmp, {f"{vid(i)}.json": (10 - i, 10) for i in range(1, 7)})  # 1 is oldest
    result = tr.cleanup_store(isolated_tmp)
    assert names(isolated_tmp) == [f"{vid(i)}.json" for i in (4, 5, 6)]
    assert result["over_limit"] == 3 and result["expired"] == 0


def test_size_limit_removes_oldest_first(tr, isolated_tmp, monkeypatch):
    monkeypatch.setenv("TUBEGPT_MAX_STORE_MB", "1")
    make_store(tr, isolated_tmp, {f"{vid(i)}.json": (10 - i, 600_000) for i in range(1, 4)})
    tr.cleanup_store(isolated_tmp)
    assert names(isolated_tmp) == [f"{vid(3)}.json"]          # 3 x 600 kB > 1 MB, so two oldest go


def test_recently_used_files_are_never_deleted_even_over_limit(tr, isolated_tmp, monkeypatch):
    monkeypatch.setenv("TUBEGPT_MAX_FILES", "1")
    now = time.time()
    make_store(tr, isolated_tmp, {f"{vid(i)}.json": (0.01, 10) for i in range(1, 4)}, now)   # 36 s old
    tr.cleanup_store(isolated_tmp, now=now)
    assert len(names(isolated_tmp)) == 3                       # protected by the in-use grace period


def test_keep_list_protects_a_file(tr, isolated_tmp):
    make_store(tr, isolated_tmp, {f"{vid(1)}.json": (500, 10)})
    tr.cleanup_store(isolated_tmp, keep=[isolated_tmp / f"{vid(1)}.json"])
    assert names(isolated_tmp) == [f"{vid(1)}.json"]


def test_dry_run_deletes_nothing(tr, isolated_tmp):
    make_store(tr, isolated_tmp, {f"{vid(1)}.json": (48, 10)})
    result = tr.cleanup_store(isolated_tmp, dry_run=True)
    assert names(isolated_tmp) == [f"{vid(1)}.json"] and result["expired"] == 1 and result["dry_run"]


@pytest.mark.parametrize("env,value", [
    ("TUBEGPT_RETENTION_HOURS", "abc"), ("TUBEGPT_RETENTION_HOURS", "nan"), ("TUBEGPT_RETENTION_HOURS", "inf"),
    ("TUBEGPT_RETENTION_HOURS", "0"), ("TUBEGPT_RETENTION_HOURS", "-5"), ("TUBEGPT_RETENTION_HOURS", "99999"),
    ("TUBEGPT_MAX_STORE_MB", "0"), ("TUBEGPT_MAX_STORE_MB", "1.5"), ("TUBEGPT_MAX_STORE_MB", "-1"),
    ("TUBEGPT_MAX_FILES", "0"), ("TUBEGPT_MAX_FILES", "x"), ("TUBEGPT_MAX_FILES", "100000"),
])
def test_invalid_storage_settings_give_clear_error(run, tmp_path, docs, monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    code, _, err = run("parse", str(f), "--video", VID)
    assert code == 2 and env in err and "Traceback" not in err


def test_blank_setting_means_default(tr, monkeypatch):
    monkeypatch.setenv("TUBEGPT_RETENTION_HOURS", "  ")
    assert tr.storage_limits()[0] == tr.DEFAULT_RETENTION_HOURS * 3600


# ---------------------------------------------------------------- only ever deletes our own files
def test_unrelated_names_and_folders_are_left_alone(tr, isolated_tmp):
    make_store(tr, isolated_tmp, {"notes.json": (500, 5), "README.txt": (500, 5), "short.json": (500, 5),
                                  f"{vid(1)}.json.bak": (500, 5), f"{vid(1)}.JSON": (500, 5)})
    folder = isolated_tmp / f"{vid(2)}.json"                      # a folder that looks like a transcript file
    folder.mkdir()
    (folder / "inside.txt").write_text("keep me")
    old = time.time() - 900 * HOUR
    os.utime(folder, (old, old))
    tr.cleanup_store(isolated_tmp)
    assert set(names(isolated_tmp)) == {"notes.json", "README.txt", "short.json", f"{vid(1)}.json.bak",
                                        f"{vid(1)}.JSON", f"{vid(2)}.json"}
    assert (folder / "inside.txt").read_text() == "keep me"


@posix_only
def test_symlinks_are_never_followed_or_deleted(tr, isolated_tmp, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("precious")
    make_store(tr, isolated_tmp, {})
    link = isolated_tmp / f"{vid(1)}.json"
    link.symlink_to(outside)
    os.utime(link, (1, 1), follow_symlinks=False)
    tr.cleanup_store(isolated_tmp)
    assert outside.read_text() == "precious" and link.is_symlink()


def test_files_outside_the_store_are_never_touched(tr, isolated_tmp, tmp_path):
    sibling = tmp_path / f"{vid(9)}.json"                            # next to the store, not in it
    sibling.write_text("outside")
    os.utime(sibling, (1, 1))
    make_store(tr, isolated_tmp, {f"{vid(1)}.json": (900, 5)})
    tr.cleanup_store(isolated_tmp)
    assert sibling.read_text() == "outside"


def test_cleanup_does_not_recurse(tr, isolated_tmp):
    make_store(tr, isolated_tmp, {})
    sub = isolated_tmp / "sub"
    sub.mkdir()
    deep = sub / f"{vid(1)}.json"
    deep.write_text("nested")
    os.utime(deep, (1, 1))
    tr.cleanup_store(isolated_tmp)
    assert deep.exists()


def test_stale_temp_files_are_removed_but_fresh_ones_kept(tr, isolated_tmp):
    make_store(tr, isolated_tmp, {".tmp-abcd1234.json": (1, 5), ".tmp-wxyz9876.json": (0, 5)})
    tr.cleanup_store(isolated_tmp)
    assert names(isolated_tmp) == [".tmp-wxyz9876.json"]


def test_custom_storage_folder_needs_marker_before_any_deletion(tr, tmp_path, monkeypatch):
    mine = tmp_path / "my-documents"
    mine.mkdir()
    foreign = mine / "my_data_001.json"                              # 11-character stem: looks like a video ID
    foreign.write_text("user data")
    os.utime(foreign, (1, 1))
    monkeypatch.setenv("TUBEGPT_STORAGE_DIR", str(mine))
    monkeypatch.setattr(tr, "default_dir", lambda: mine)
    tr.ensure_store()                                                # non-empty folder without marker: no adoption
    assert not (mine / tr.STORE_MARKER).exists()
    tr.cleanup_store(mine)
    assert foreign.read_text() == "user data"


def test_custom_storage_folder_created_by_us_gets_marker_and_cleanup_works(tr, tmp_path, monkeypatch):
    mine = tmp_path / "fresh-store"
    monkeypatch.setenv("TUBEGPT_STORAGE_DIR", str(mine))
    monkeypatch.setattr(tr, "default_dir", lambda: mine)
    tr.ensure_store()
    assert (mine / tr.STORE_MARKER).is_file()
    make_store(tr, mine, {f"{vid(1)}.json": (48, 5)})
    tr.cleanup_store(mine)
    assert not (mine / f"{vid(1)}.json").exists()


# ---------------------------------------------------------------- automatic cleanup and usage
def test_saving_runs_cleanup_automatically(run, tr, tmp_path, docs, isolated_tmp):
    make_store(tr, isolated_tmp, {f"{vid(1)}.json": (48, 10)})
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    assert run("parse", str(f), "--video", VID)[0] == 0
    assert names(isolated_tmp) == [f"{VID}.json"]


def test_files_given_with_out_are_never_cleaned(run, tr, tmp_path, docs, isolated_tmp):
    keep = tmp_path / f"{vid(5)}.json"
    keep.write_text("mine")
    os.utime(keep, (1, 1))
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    run("parse", str(f), "--video", VID)                              # default store: triggers cleanup
    run("parse", str(f), "--out", str(tmp_path / "x.json"))
    assert keep.read_text() == "mine"


def test_loading_a_stored_transcript_counts_as_use(run, tr, tmp_path, docs, isolated_tmp):
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    run("parse", str(f), "--video", VID)
    stored = isolated_tmp / f"{VID}.json"
    os.utime(stored, (time.time() - 30 * HOUR, time.time() - 30 * HOUR))
    assert run("show", VID)[0] == 0                                   # reading it refreshes it
    tr.cleanup_store(isolated_tmp)
    assert stored.exists()


def test_cleanup_command(run, tr, isolated_tmp):
    code, out, _ = run("cleanup")
    assert code == 0 and "does not exist" in out
    make_store(tr, isolated_tmp, {f"{vid(1)}.json": (48, 10), f"{vid(2)}.json": (1, 10)})
    code, out, _ = run("cleanup", "--dry-run")
    assert code == 0 and "Would remove 1 expired" in out and len(names(isolated_tmp)) == 2
    code, out, _ = run("cleanup")
    assert code == 0 and "Removed 1 expired" in out and names(isolated_tmp) == [f"{vid(2)}.json"]


# ---------------------------------------------------------------- atomic and safe writes
def test_failed_write_leaves_no_temp_file_and_keeps_previous_content(run, tr, tmp_path, docs, isolated_tmp, monkeypatch):
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    out = tmp_path / "doc.json"
    assert run("parse", str(f), "--out", str(out))[0] == 0
    before = out.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(tr.os, "replace", boom)
    code, _, err = run("parse", str(f), "--out", str(out))
    assert code == 7 and "free disk space" in err
    assert out.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".tmp-")] == []


@pytest.mark.parametrize("err_no,fragment", [
    (errno.ENOSPC, "free disk space"), (errno.EROFS, "read-only"), (errno.EACCES, "Permission was denied"),
    (errno.EIO, "operation failed"),
])
def test_storage_errors_have_clear_messages_and_code_7(run, tr, tmp_path, docs, isolated_tmp, monkeypatch, err_no, fragment):
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError(err_no, "boom")
    monkeypatch.setattr(tr.tempfile, "mkstemp", boom)
    code, _, err = run("parse", str(f), "--video", VID)
    assert code == 7 and fragment in err and "TUBEGPT_STORAGE_DIR" in err and "Traceback" not in err
    assert str(tmp_path) not in err                                   # no local paths in the message


def test_storage_folder_that_is_a_file_is_reported(run, tr, tmp_path, docs, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a folder")
    monkeypatch.setattr(tr, "default_dir", lambda: blocker)
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    code, _, err = run("parse", str(f), "--video", VID)
    assert code == 7 and "not a folder" in err


@posix_only
def test_symlinked_default_store_is_refused(run, tr, tmp_path, docs, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setattr(tr, "default_dir", lambda: link)
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    code, _, err = run("parse", str(f), "--video", VID)
    assert code == 7 and "symbolic link" in err and list(real.iterdir()) == []


@posix_only
def test_store_owned_by_another_user_is_refused(run, tr, tmp_path, docs, isolated_tmp, monkeypatch):
    isolated_tmp.mkdir()
    monkeypatch.setattr(tr.os, "getuid", lambda: os.stat(isolated_tmp).st_uid + 1)
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    code, _, err = run("parse", str(f), "--video", VID)
    assert code == 7 and "another user" in err


@posix_only
def test_store_and_files_are_private(run, tr, tmp_path, docs, isolated_tmp):
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    run("parse", str(f), "--video", VID)
    assert oct(isolated_tmp.stat().st_mode & 0o777) == "0o700"
    assert oct((isolated_tmp / f"{VID}.json").stat().st_mode & 0o777) == "0o600"


@posix_only
def test_group_writable_store_is_tightened(run, tr, tmp_path, docs, isolated_tmp):
    isolated_tmp.mkdir()
    isolated_tmp.chmod(0o777)
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    assert run("parse", str(f), "--video", VID)[0] == 0
    assert isolated_tmp.stat().st_mode & 0o022 == 0


def test_out_pointing_to_a_folder_is_rejected(run, tmp_path, docs):
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    code, _, err = run("parse", str(f), "--out", str(tmp_path))
    assert code == 2 and "folder" in err


def test_oversized_document_is_refused_before_writing(run, tr, tmp_path, docs, monkeypatch):
    monkeypatch.setattr(tr, "MAX_DOC_BYTES", 50)
    f = tmp_path / "a.srt"
    f.write_text(docs.SRT, encoding="utf-8")
    out = tmp_path / "doc.json"
    code, _, err = run("parse", str(f), "--out", str(out))
    assert code == 2 and "too large" in err and not out.exists()


# ---------------------------------------------------------------- path traversal
@pytest.mark.parametrize("src", ["../../etc/passwd", "..\\..\\windows\\win.ini", "/etc/passwd/..", "a/../../b",
                                 "%2e%2e%2f", "....//....//x", "dQw4w9WgXc/", "\x00"])
def test_traversal_style_sources_never_read_outside_the_store(run, src):
    code, out, err = run("show", src)
    assert code == 2 and out == "" and "Traceback" not in err


@pytest.mark.parametrize("name", ["../x.json", "a/b.json", "..", "", f"{VID}.json/../x", "x" * 11 + ".txt",
                                  "/abs/path.json", f"{VID}\\x.json"])
def test_store_file_accepts_only_generated_names(tr, name):
    with pytest.raises(SystemExit) as exc:
        tr.store_file(name)
    assert exc.value.code == 2


def test_programmatic_save_with_hostile_video_id_is_refused(tr, tmp_path, isolated_tmp):
    doc = {"video_id": "../../evil", "segments": [{"start": None, "text": "x"}]}
    with pytest.raises(SystemExit) as exc:
        tr.save_doc(doc, None)
    assert exc.value.code == 2
    assert not (tmp_path / "evil.json").exists()


def test_hash_named_store_files_match_what_cleanup_expects(tr, run, tmp_path, docs, isolated_tmp):
    f = tmp_path / "n.txt"
    f.write_text(docs.PLAIN, encoding="utf-8")
    run("parse", str(f))
    (name,) = [n for n in names(isolated_tmp)]
    assert tr.STORE_FILE_RE.match(name)


def test_cleanup_command_says_so_when_it_skips_an_unmarked_custom_folder(run, tr, tmp_path, monkeypatch):
    mine = tmp_path / "my-own-folder"
    mine.mkdir()
    (mine / "keepme.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("TUBEGPT_STORAGE_DIR", str(mine))
    monkeypatch.setattr(tr, "default_dir", lambda: mine)    # the shared fixture redirects the store per test
    code, out, _ = run("cleanup")
    assert code == 0 and "Cleanup skipped" in out and "none of its files were touched" in out
    assert [p.name for p in mine.iterdir()] == ["keepme.json"]
