import importlib.util
import sys
import types
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "youtube-conversation" / "scripts" / "transcript.py"


@pytest.fixture(scope="session")
def tr():
    spec = importlib.util.spec_from_file_location("transcript_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["transcript_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def isolated_tmp(tr, tmp_path, monkeypatch):
    """Every test saves transcripts inside its own temp dir, never the real one."""
    store = tmp_path / "store"
    monkeypatch.setattr(tr, "default_dir", lambda: store)
    # ordinary tests must never touch the network
    monkeypatch.setattr(tr, "try_oembed", lambda vid: ("Mock title", "Mock channel"))
    return store


@pytest.fixture
def run(tr, capsys):
    """Run the CLI in-process; returns (exit_code, stdout, stderr)."""
    def _run(*argv):
        try:
            code = tr.main(list(argv))
        except SystemExit as exc:
            code = exc.code
        out = capsys.readouterr()
        return code, out.out, out.err
    return _run


SRT = """1
00:00:01,000 --> 00:00:03,500
Hello and <i>welcome</i> to the show.

2
00:00:03,500 --> 00:00:07,000
Today we talk about interest rates &amp; inflation.

3
00:01:05,250 --> 00:01:09,000
Rates went up again.
"""

VTT = """WEBVTT
Kind: captions

00:00:00.000 --> 00:00:02.000
Welcome back.

00:00:02.000 --> 00:00:04.000
Welcome back.
This line is new.

01:02:03.000 --> 01:02:05.000
An hour in, we discuss <c>caching</c>.
"""

PANEL = """Transcript
0:00 Intro to the topic
0:15
second thought spans
two lines
1:02:03 - Late segment
"""

PLAIN = "First line of notes.\nSecond line of notes.\n\nThird line mentions Python."


@pytest.fixture
def docs():
    return types.SimpleNamespace(SRT=SRT, VTT=VTT, PANEL=PANEL, PLAIN=PLAIN)


VID = "dQw4w9WgXcQ"
