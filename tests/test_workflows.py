"""GitHub Actions supply-chain and least-privilege rules, checked on the workflow files themselves.

The workflows are read as text (no YAML library is needed); the rules are simple on purpose so a reviewer can
see exactly what is enforced.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def read(path):
    return path.read_text(encoding="utf-8")


def job_blocks(text):
    """{job name: text of the job}, taken from the two-space-indented keys under 'jobs:'."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([A-Za-z0-9_-]+):\s*$", body, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def test_there_are_workflows_to_check():
    assert {p.name for p in WORKFLOWS} >= {"tests.yml", "security.yml"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_full_commit_sha_with_a_version_comment(path):
    uses = [ln.strip() for ln in read(path).splitlines() if re.match(r"\s*-?\s*uses:", ln)]
    assert uses, "expected at least one action"
    for line in uses:
        assert re.search(r"uses:\s*[\w.-]+/[\w./-]+@[0-9a-f]{40}\s+#\s*v\d+(\.\d+)*\s*$", line), \
            f"not pinned to a commit SHA with a '# vX.Y.Z' comment: {line}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_token_defaults_to_no_permissions_and_no_job_can_write(path):
    text = read(path)
    assert re.search(r"^permissions:\s*\{\}\s*(#.*)?$", text, re.M), "top-level permissions must be '{}'"
    assert not re.search(r":\s*write\b", text), "no job may request write access"
    for name, block in job_blocks(text).items():
        assert re.search(r"^    permissions:", block, re.M), f"job {name} must state its permissions"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_job_has_a_timeout(path):
    for name, block in job_blocks(read(path)).items():
        assert re.search(r"^    timeout-minutes:\s*\d+\s*$", block, re.M), f"job {name} has no timeout-minutes"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_checkout_never_persists_credentials(path):
    text = read(path)
    checkouts = len(re.findall(r"uses:\s*actions/checkout@", text))
    assert checkouts == len(re.findall(r"persist-credentials:\s*false", text))


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_privileged_triggers_secrets_or_script_injection(path):
    text = read(path)
    assert "pull_request_target" not in text and "workflow_run" not in text
    assert "secrets." not in text, "these workflows must not need any secret"
    for dangerous in ("github.event.", "github.head_ref", "inputs."):
        assert f"${{{{ {dangerous}" not in text and f"${{{{{dangerous}" not in text, \
            f"untrusted context {dangerous} must not be expanded in a workflow"


def test_the_test_matrix_matches_the_supported_python_versions():
    text = read(ROOT / ".github" / "workflows" / "tests.yml")
    versions = re.search(r"test-linux:.*?python-version:\s*\[([^\]]+)\]", text, re.S).group(1)
    assert [v.strip(' "') for v in versions.split(",")] == ["3.11", "3.12", "3.13", "3.14", "3.15"]
    assert "windows-" in text and "macos-" in text, "Windows and macOS must be covered"
    assert not re.search(r'"3\.(9|10)"', text), "end-of-life Python versions must not be tested"


def test_security_workflow_has_the_scheduled_scans():
    text = read(ROOT / ".github" / "workflows" / "security.yml")
    assert re.search(r"schedule:\s*\n\s*-\s*cron:", text), "vulnerability checks need a schedule"
    assert "gitleaks" in text and "pip-audit" in text and "fetch-depth: 0" in text
    assert "--redact" in text, "secret values must never be printed"


def test_dependabot_covers_python_packages_and_actions():
    text = read(ROOT / ".github" / "dependabot.yml")
    assert 'package-ecosystem: "pip"' in text and 'package-ecosystem: "github-actions"' in text
    assert "interval:" in text
