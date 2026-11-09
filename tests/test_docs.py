"""Documentation must match the implementation, and its internal links must work."""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "youtube-conversation" / "scripts" / "transcript.py"
DOCS = ["README.md", "index.md", "about.md", "privacy.md", "terms.md", "support.md",
        "skills/youtube-conversation/SKILL.md"]
LINK_RE = re.compile(r"\]\(([^)\s]+)\)")


def slugs(markdown_text):
    found = set()
    for line in markdown_text.splitlines():
        m = re.match(r"^#{1,6}\s+(.*?)\s*#*$", line)
        if m:
            slug = re.sub(r"[^\w\s-]", "", m.group(1).lower()).strip().replace(" ", "-")
            found.add(slug)
    return found


@pytest.mark.parametrize("doc", DOCS)
def test_documentation_files_exist(doc):
    assert (ROOT / doc).is_file()


@pytest.mark.parametrize("doc", DOCS)
def test_relative_links_and_anchors_resolve(doc):
    path = ROOT / doc
    text = path.read_text(encoding="utf-8")
    for target in LINK_RE.findall(text):
        if re.match(r"^(https?:|mailto:)", target):
            continue
        file_part, _, anchor = target.partition("#")
        if file_part:
            # The GitHub Pages site serves page.md as page.html, so either spelling is a valid link.
            candidate = (path.parent / file_part)
            if candidate.suffix == ".html":
                candidate = candidate.with_suffix(".md")
            assert candidate.is_file(), f"{doc}: broken link to {target}"
            target_text = candidate.read_text(encoding="utf-8") if candidate.suffix == ".md" else ""
        else:
            target_text = text
        if anchor and target_text:
            assert anchor in slugs(target_text), f"{doc}: no heading for anchor #{anchor}"


def test_no_placeholder_text_in_public_docs():
    for doc in DOCS[:6]:
        text = (ROOT / doc).read_text(encoding="utf-8")
        for marker in ("your-email", "example.com", "TODO", "TBD", "lorem ipsum", "[email]", "<your"):
            assert marker.lower() not in text.lower(), f"{doc} contains placeholder text {marker!r}"


def test_unsafe_install_commands_only_appear_as_warnings():
    for doc in DOCS + ["requirements.txt", "requirements-dev.txt", "constraints.txt"]:
        for number, line in enumerate((ROOT / doc).read_text(encoding="utf-8").splitlines(), 1):
            if "--break-system-packages" in line or "sudo pip" in line:
                assert re.search(r"\b(never|not|no|don't|do not|without)\b", line, re.I), \
                    f"{doc}:{number} recommends an unsafe install command"


def test_every_environment_setting_in_the_code_is_documented():
    names = set(re.findall(r"TUBEGPT_[A-Z_]+", SCRIPT.read_text(encoding="utf-8")))
    assert {"TUBEGPT_STORAGE_DIR", "TUBEGPT_RETENTION_HOURS", "TUBEGPT_MAX_STORE_MB", "TUBEGPT_MAX_FILES"} <= names
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in names:
        assert name in readme, f"{name} is not documented in README.md"


def test_every_cli_command_is_documented():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    commands = {n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call)
                and getattr(n.func, "attr", "") == "add_parser" and n.args and isinstance(n.args[0], ast.Constant)}
    assert commands == {"fetch", "parse", "show", "search", "link", "cleanup"}
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    skill = (ROOT / "skills/youtube-conversation/SKILL.md").read_text(encoding="utf-8")
    for command in commands:
        assert f"transcript.py {command}" in readme or f"transcript.py -v {command}" in readme or command in readme
        if command != "cleanup":
            assert f"transcript.py {command}" in skill, f"SKILL.md does not show the {command} command"


def test_every_exit_code_is_documented_in_the_script_docstring_and_readme():
    source = SCRIPT.read_text(encoding="utf-8")
    codes = {int(c) for c in re.findall(r"^EXIT_\w+(?:, EXIT_\w+)*\s*=\s*([\d, ]+)$", source, re.M)
             for c in c.split(",")}
    codes |= {4, 5, 6, 7}
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for code in sorted(codes):
        assert re.search(rf"\b{code}\b", readme), f"exit code {code} is not documented in README.md"
