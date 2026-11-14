"""Repository hygiene and security regression checks (no network, no git needed)."""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "youtube-conversation" / "scripts" / "transcript.py"
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv", "node_modules"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".toml", ".ini", ".yml", ".yaml", ".cfg", ".html", ".gitignore"}

SECRET_PATTERNS = {
    "GitHub token": r"gh[pousr]_[A-Za-z0-9]{36,}",
    "GitHub fine-grained token": r"github_pat_[A-Za-z0-9_]{22,}",
    "OpenAI/Anthropic style key": r"sk-[A-Za-z0-9_-]{24,}",
    "AWS access key id": r"AKIA[0-9A-Z]{16}",
    "Google API key": r"AIza[0-9A-Za-z_-]{35}",
    "Slack token": r"xox[abprs]-[A-Za-z0-9-]{10,}",
    "private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "bearer token": r"[Aa]uthorization:\s*[Bb]earer\s+[A-Za-z0-9._~+/-]{20,}",
}
FORBIDDEN_NAMES = re.compile(r"^(\.env(\..*)?|.*\.pem|.*\.key|.*\.p12|.*\.pfx|id_rsa.*|id_ed25519.*|"
                             r"credentials.*\.json|.*cookies.*\.txt|\.netrc|.*\.sqlite3?|.*\.log)$", re.I)


def project_files():
    for path in ROOT.rglob("*"):
        if path.is_file() and not (set(path.relative_to(ROOT).parts) & SKIP_DIRS):
            yield path


def test_no_secrets_in_any_project_file():
    compiled = {name: re.compile(rx) for name, rx in SECRET_PATTERNS.items()}
    for path in project_files():
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name != ".gitignore":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for name, rx in compiled.items():
            assert not rx.search(text), f"possible {name} in {path.relative_to(ROOT)}"


def test_no_secret_bearing_or_local_files_in_the_tree():
    offenders = [str(p.relative_to(ROOT)) for p in project_files() if FORBIDDEN_NAMES.match(p.name)]
    assert not offenders, f"files that should not be in the repository: {offenders}"


def test_gitignore_excludes_local_and_secret_files():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".venv", "__pycache__", ".pytest_cache", ".env", ".ruff_cache"):
        assert re.search(rf"^{re.escape(pattern)}", text, re.M), f".gitignore does not exclude {pattern}"


@pytest.fixture(scope="module")
def tree():
    return ast.parse(SCRIPT.read_text(encoding="utf-8"))


def _imports_outside(tree, allowed_function):
    """Top-level names imported anywhere except inside the one function ``allowed_function``."""
    inside = set()
    for func in ast.walk(tree):
        if isinstance(func, ast.FunctionDef) and func.name == allowed_function:
            inside |= {id(node) for node in ast.walk(func)}
    imported = set()
    for node in ast.walk(tree):
        if id(node) in inside:
            continue
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def test_script_has_no_dangerous_imports(tree):
    banned = {"subprocess", "pickle", "marshal", "shelve", "ctypes", "socket", "ftplib", "telnetlib", "pty"}
    # ctypes is allowed in exactly one place: _win_api, the Windows-only function that reads file owners and deletes
    # through an open handle (the standard library has no other way). Anywhere else it is still banned.
    imported = _imports_outside(tree, "_win_api")
    assert not imported & banned, f"unexpected imports: {imported & banned}"


def test_ctypes_is_imported_only_inside_the_windows_helper(tree):
    users = [func.name for func in ast.walk(tree) if isinstance(func, ast.FunctionDef)
             and any(isinstance(n, (ast.Import, ast.ImportFrom)) and "ctypes" in ast.dump(n) for n in ast.walk(func))]
    assert users == ["_win_api"], users


def test_script_has_no_dynamic_code_execution_or_shell(tree):
    banned_calls = {"eval", "exec", "compile", "__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if isinstance(func, ast.Name):  # bare eval(...)/exec(...); re.compile(...) is an attribute call and fine
                assert name not in banned_calls, f"call to {name} at line {node.lineno}"
            if isinstance(func, ast.Attribute) and getattr(func.value, "id", "") == "builtins":
                assert name not in banned_calls, f"call to builtins.{name} at line {node.lineno}"
            assert name not in {"system", "popen", "spawn", "Popen", "run"} or not isinstance(func, ast.Attribute) \
                or getattr(func.value, "id", "") != "os", f"shell call at line {node.lineno}"
            for kw in node.keywords:
                assert not (kw.arg == "shell" and getattr(kw.value, "value", False) is True)


def test_script_makes_exactly_one_direct_network_call_to_youtube(tree):
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "urlopen"]
    assert len(calls) == 1, "the script should have exactly one urlopen call (the YouTube oEmbed lookup)"
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'target = "https://www.youtube.com/oembed?' in source


def test_script_only_mentions_youtube_hosts():
    allowed = {"youtube.com", "www.youtube.com", "youtu.be", "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com"}
    text = SCRIPT.read_text(encoding="utf-8")
    hosts = set(re.findall(r"https?://([A-Za-z0-9.-]+)", text))
    assert hosts <= allowed, f"unexpected hosts in the script: {hosts - allowed}"


def test_script_never_deletes_recursively(tree):
    source = SCRIPT.read_text(encoding="utf-8")
    assert "rmtree" not in source and "shutil" not in source
    deleters = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") in
                {"unlink", "remove", "rmdir"}]
    assert 1 <= len(deleters) <= 4, "file deletion should stay limited to the audited single-file calls"
