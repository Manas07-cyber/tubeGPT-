"""Regression tests from the independent storage and fetch review.

Each test names the realistic failure it guards against: a planted hard link, a Windows junction, a relative
storage setting used from another folder, a transient Windows rename failure, a FIFO that appears after a check,
a silent network connection, and ``--out`` pointing at a link.
"""
import errno
import json
import os
import stat
import sys
import threading
import types

import pytest
import requests

VID = "dQw4w9WgXcQ"
OTHER = "aaaaaaaaaaa"
HOUR = 3600
DOC = {"video_id": VID, "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}], "has_timestamps": True}
posix_only = pytest.mark.skipif(os.name != "posix", reason="needs POSIX permissions, links and user IDs")


def write_doc(path, doc=DOC, age_h=0.0, perm=None):
    path.write_text(json.dumps(doc), encoding="utf-8")
    import time
    t = time.time() - age_h * HOUR
    os.utime(path, (t, t))
    if perm is not None:
        os.chmod(path, perm)
    return path


# --------------------------------------------------------------------------- hard links
@posix_only
def test_a_planted_hard_link_in_the_store_is_not_read_chmodded_or_deleted(run, tr, isolated_tmp, tmp_path):
    """A second name for somebody's file must not be used, re-permissioned (that would change the original) or removed."""
    isolated_tmp.mkdir(mode=0o700)
    original = write_doc(tmp_path / "mine.json", {**DOC, "segments": [{"start": 0.0, "text": "PRIVATE"}]},
                         age_h=900, perm=0o644)
    os.link(original, isolated_tmp / f"{VID}.json")
    code, out, err = run("show", VID)
    assert code == tr.EXIT_STORAGE and "hard link" in err and "PRIVATE" not in out
    tr.secure_store_files(isolated_tmp)
    assert stat.S_IMODE(os.stat(original).st_mode) == 0o644, "the other name's permissions must not be changed"
    result = tr.cleanup_store(isolated_tmp)
    assert (isolated_tmp / f"{VID}.json").exists() and original.exists() and result["expired"] == 0


@posix_only
def test_a_hard_link_to_a_foreign_file_cannot_be_deleted_through_a_later_swap(tr, isolated_tmp, tmp_path, monkeypatch):
    """The re-check right before deletion also refuses a file that gained a second name after the scan."""
    isolated_tmp.mkdir(mode=0o700)
    target = write_doc(isolated_tmp / f"{VID}.json", age_h=900)
    real_remove = tr._remove_if_unchanged

    def link_then_remove(store, dirfd, entry, now, min_age):
        os.link(target, tmp_path / "second-name.json")  # happens after the scan, before the delete
        return real_remove(store, dirfd, entry, now, min_age)

    monkeypatch.setattr(tr, "_remove_if_unchanged", link_then_remove)
    tr.cleanup_store(isolated_tmp)
    assert target.exists()


# --------------------------------------------------------------------------- Windows junction and rename retry
def test_a_windows_junction_is_refused_as_a_storage_folder(tr, isolated_tmp, monkeypatch, capsys):
    """Junctions are not symbolic links to Python's lstat, so they need their own check (simulated here)."""
    isolated_tmp.mkdir()
    real_lstat = os.lstat

    def fake_lstat(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(isolated_tmp):
            return types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=getattr(os, "getuid", lambda: 0)(),
                                         st_reparse_tag=0xA0000003)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", fake_lstat)
    with pytest.raises(SystemExit) as exc:
        tr.ensure_store(create=False)
    assert exc.value.code == tr.EXIT_STORAGE
    assert "junction" in capsys.readouterr().err


def test_a_transient_permission_error_on_rename_is_retried_on_windows_only(tr, tmp_path, monkeypatch):
    target = tmp_path / f"{VID}.json"
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(errno.EACCES, "locked by a virus scanner")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    monkeypatch.setattr(tr, "_IS_POSIX", False)  # behave as on Windows
    tr.write_json_atomic(target, DOC)
    assert calls["n"] == 3 and json.loads(target.read_text(encoding="utf-8"))["video_id"] == VID
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".tmp-")]


def test_a_persistent_permission_error_is_still_reported_and_leaves_no_temp_file(tr, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError(errno.EACCES, "locked")))
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    monkeypatch.setattr(tr, "_IS_POSIX", False)
    with pytest.raises(SystemExit) as exc:
        tr.write_json_atomic(tmp_path / f"{VID}.json", DOC)
    assert exc.value.code == tr.EXIT_STORAGE
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".tmp-")]


def test_posix_does_not_retry_a_permission_error(tr, tmp_path, monkeypatch):
    calls = {"n": 0}

    def deny(src, dst):
        calls["n"] += 1
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(os, "replace", deny)
    monkeypatch.setattr(tr, "_IS_POSIX", True)
    with pytest.raises(SystemExit):
        tr.write_json_atomic(tmp_path / f"{VID}.json", DOC)
    assert calls["n"] == 1


# --------------------------------------------------------------------------- relative storage setting
@pytest.fixture
def real_default_dir():
    """default_dir from a fresh copy of the script (the autouse fixture replaces it on the shared module)."""
    import importlib.util

    from conftest import SCRIPT
    spec = importlib.util.spec_from_file_location("transcript_fresh_for_default_dir", SCRIPT)
    fresh = importlib.util.module_from_spec(spec)
    sys.modules["transcript_fresh_for_default_dir"] = fresh
    spec.loader.exec_module(fresh)
    return fresh.default_dir


def test_a_relative_storage_setting_is_made_absolute_once(real_default_dir, tmp_path, monkeypatch):
    """A relative setting must name one folder, not whichever folder the next command happens to run in."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TUBEGPT_STORAGE_DIR", "relative-store")
    store = real_default_dir()
    assert store.is_absolute() and store == tmp_path / "relative-store"
    monkeypatch.setenv("TUBEGPT_STORAGE_DIR", "~/somewhere")
    assert real_default_dir().is_absolute() and "~" not in str(real_default_dir())


@posix_only
def test_making_the_storage_path_absolute_never_resolves_a_symbolic_link(real_default_dir, tmp_path, monkeypatch):
    """Resolving would hide a symlinked store from the symlink check, so only the lexical path is made absolute."""
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    monkeypatch.setenv("TUBEGPT_STORAGE_DIR", str(link))
    assert real_default_dir() == link and os.path.islink(real_default_dir())


# --------------------------------------------------------------------------- FIFO named explicitly
@posix_only
def test_a_fifo_named_explicitly_never_hangs_the_reader(tr, tmp_path):
    """Even if a named pipe appears after the regular-file check, opening must not block waiting for a writer."""
    fifo = tmp_path / "pipe.json"
    os.mkfifo(fifo)
    result = {}

    def attempt():
        try:
            tr.load_doc(str(fifo))
        except SystemExit as exc:
            result["code"] = exc.code

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "load_doc blocked on a FIFO"
    assert result["code"] != 0


# --------------------------------------------------------------------------- --out must not overwrite unrelated files
@pytest.fixture
def pasted(tmp_path):
    path = tmp_path / "pasted.txt"
    path.write_text("[00:01] hello world\n[00:05] second line\n", encoding="utf-8")
    return path


def parse_to(run, pasted, out, *extra):
    return run("parse", str(pasted), "--video", VID, "--out", str(out), *extra)


def test_out_refuses_to_replace_an_unrelated_file_and_leaves_it_untouched(run, tr, pasted, tmp_path):
    """A transcript that steers an agent into '--out ~/.bashrc' must not be able to overwrite the user's file."""
    profile = tmp_path / ".bashrc"
    profile.write_text("export PATH=$HOME/bin:$PATH\n", encoding="utf-8")
    code, out, err = parse_to(run, pasted, profile)
    assert code == tr.EXIT_BAD_INPUT and "not a tubeGPT transcript" in err and "--force" in err
    assert profile.read_text(encoding="utf-8") == "export PATH=$HOME/bin:$PATH\n"
    assert str(profile) not in err, "the message must not echo paths"


@pytest.mark.parametrize("content", ["not json at all", "[]", '{"segments": "nope"}', "\ufffd\ufffd", '{"video_id": 5}'])
def test_out_refuses_files_that_are_json_or_text_but_not_transcripts(run, tr, pasted, tmp_path, content):
    target = tmp_path / "other.json"
    target.write_text(content, encoding="utf-8")
    code, _, _ = parse_to(run, pasted, target)
    assert code == tr.EXIT_BAD_INPUT and target.read_text(encoding="utf-8") == content


def test_out_may_create_a_new_file_fill_an_empty_one_and_replace_an_earlier_transcript(run, tr, pasted, tmp_path):
    new = tmp_path / "new.json"
    assert parse_to(run, pasted, new)[0] == 0 and json.loads(new.read_text(encoding="utf-8"))["video_id"] == VID
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    assert parse_to(run, pasted, empty)[0] == 0 and json.loads(empty.read_text(encoding="utf-8"))["video_id"] == VID
    assert parse_to(run, pasted, new)[0] == 0, "running the same command again must keep working"
    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + json.dumps(DOC).encode("utf-8"))
    assert parse_to(run, pasted, bom)[0] == 0


def test_force_is_the_explicit_way_to_replace_an_unrelated_file(run, tr, pasted, tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("my notes", encoding="utf-8")
    assert parse_to(run, pasted, target)[0] == tr.EXIT_BAD_INPUT
    assert parse_to(run, pasted, target, "--force")[0] == 0
    assert json.loads(target.read_text(encoding="utf-8"))["video_id"] == VID


def test_out_refuses_a_file_larger_than_a_transcript_can_be(run, tr, pasted, tmp_path, monkeypatch):
    target = tmp_path / "big.bin"
    target.write_bytes(b"x" * 500)
    monkeypatch.setattr(tr, "MAX_DOC_BYTES", 100)
    assert tr._out_may_replace(target) is False


@posix_only
def test_out_pointing_at_a_symlink_is_refused_and_with_force_replaces_only_the_link(run, tr, pasted, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("IMPORTANT", encoding="utf-8")
    link = tmp_path / "out.json"
    link.symlink_to(victim)
    assert parse_to(run, pasted, link)[0] == tr.EXIT_BAD_INPUT
    assert link.is_symlink() and victim.read_text(encoding="utf-8") == "IMPORTANT"
    assert parse_to(run, pasted, link, "--force")[0] == 0
    assert victim.read_text(encoding="utf-8") == "IMPORTANT", "never written through the link, even with --force"
    assert not link.is_symlink() and json.loads(link.read_text(encoding="utf-8"))["video_id"] == VID


@posix_only
def test_out_pointing_at_a_pipe_is_refused_without_blocking(tr, tmp_path):
    fifo = tmp_path / "pipe.json"
    os.mkfifo(fifo)
    assert tr._out_may_replace(fifo) is False


def test_out_refuses_when_the_target_cannot_be_inspected_or_opened_safely(tr, tmp_path, monkeypatch):
    """If the check itself fails, the answer is no: an unreadable verdict must never become permission to overwrite."""
    target = tmp_path / "doc.json"
    target.write_text(json.dumps(DOC), encoding="utf-8")
    assert tr._out_may_replace(target) is True
    monkeypatch.setattr(tr, "_open_regular", lambda *a, **k: (_ for _ in ()).throw(tr.UnsafeStoreEntry("hard link")))
    assert tr._out_may_replace(target) is False
    monkeypatch.undo()
    real_lstat = os.lstat

    def broken(path, *a, **k):
        if os.fspath(path) == str(target):
            raise PermissionError(13, "denied")
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(os, "lstat", broken)
    assert tr._out_may_replace(target) is False


def test_out_still_refuses_a_folder(run, tr, pasted, tmp_path):
    folder = tmp_path / "dir"
    folder.mkdir()
    code, _, err = parse_to(run, pasted, folder, "--force")
    assert code == tr.EXIT_BAD_INPUT and "folder" in err


def test_the_help_text_documents_force(run):
    for command in ("fetch", "parse"):
        _, out, err = run(command, "--help")
        assert "--force" in out + err


# --------------------------------------------------------------------------- network timeout
def _api_class(captured):
    class Api:
        def __init__(self, proxy_config=None, http_client=None):
            captured["client"] = http_client

    return Api


def test_the_transcript_client_gets_a_session_that_adds_a_timeout(tr):
    captured = {}
    tr.make_api(_api_class(captured))
    session = captured["client"]
    assert isinstance(session, requests.Session)
    seen = {}

    class Recorder(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            seen.update(kwargs)
            response = requests.Response()
            response.status_code = 200
            response._content = b"{}"
            return response

        def close(self):
            pass

    session.mount("https://", Recorder())
    session.get("https://www.youtube.com/watch?v=" + VID)
    assert seen["timeout"] == tr.FETCH_TIMEOUT_S
    session.get("https://www.youtube.com/", timeout=3)
    assert seen["timeout"] == 3, "an explicit timeout from the library is respected"


def test_an_api_class_without_a_client_option_is_still_created(tr):
    class Old:
        def __init__(self):
            self.ok = True

    assert tr.make_api(Old).ok


def test_fetch_uses_the_timeout_client(tr, monkeypatch, run):
    """The fetch command builds its client through make_api, so the timeout cannot be bypassed."""
    module = types.ModuleType("youtube_transcript_api")

    class Api:
        def __init__(self, proxy_config=None, http_client=None):
            built.append(http_client)

        def list(self, vid):
            raise RuntimeError("stop here: only the client construction is under test")

        def fetch(self, *a, **k):
            pass

    built = []
    module.YouTubeTranscriptApi = Api
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", module)
    code, _, _ = run("fetch", VID)
    assert code == tr.EXIT_ERROR
    assert len(built) == 1 and isinstance(built[0], requests.Session)


def test_the_timeout_is_a_sane_value(tr):
    assert 5 <= tr.FETCH_TIMEOUT_S <= 60
