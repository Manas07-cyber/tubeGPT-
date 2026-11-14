"""Optional live test against the real YouTube. Disabled by default; never part of ordinary CI.

Run it deliberately with:   TUBEGPT_RUN_INTEGRATION=1 python -m pytest -m integration -v -rs
It makes one normal caption request for one public video, with no cookies or credentials. If YouTube is
unreachable or refuses the request (common from cloud addresses) the test is reported as skipped with the
reason, because that is an environment limit, not a code defect.
"""
import json
import os

import pytest

PUBLIC_VIDEO = "https://youtu.be/dQw4w9WgXcQ"          # public video with captions, used by the project's smoke test

pytestmark = pytest.mark.integration

if os.environ.get("TUBEGPT_RUN_INTEGRATION") != "1":
    pytest.skip("integration tests are disabled; set TUBEGPT_RUN_INTEGRATION=1 to run them against the real YouTube",
                allow_module_level=True)


def test_real_fetch_of_a_public_captioned_video(tr, run, tmp_path):
    pytest.importorskip("youtube_transcript_api", reason="youtube-transcript-api is not installed")
    out = tmp_path / "live.json"
    code, stdout, stderr = run("fetch", PUBLIC_VIDEO, "--out", str(out))
    if code in (tr.EXIT_NETWORK, tr.EXIT_BLOCKED):
        pytest.skip(f"YouTube was not reachable from this environment (exit {code}): {stderr.strip()[:200]}")
    assert code == 0, stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    tr.validate_doc(doc)                                  # what the real library returns satisfies our own validation
    assert doc["video_id"] == "dQw4w9WgXcQ" and len(doc["segments"]) > 10 and doc["has_timestamps"]
    code, shown, _ = run("show", str(out), "--max-chars", "400")
    assert code == 0 and shown.startswith("[0:")
    code, linked, _ = run("link", str(out), "1:05")
    assert code == 0 and "youtu.be/dQw4w9WgXcQ?t=65" in linked
