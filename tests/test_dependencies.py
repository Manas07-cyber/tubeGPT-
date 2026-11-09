"""Dependency declarations must match what the code really imports, with bounded versions."""
import ast
import importlib.util
import re
import sys
import sysconfig
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "youtube-conversation" / "scripts" / "transcript.py"
IMPORT_TO_DIST = {"youtube_transcript_api": "youtube-transcript-api", "requests": "requests"}


def requirement_lines(name):
    lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def top_level_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def is_stdlib_by_location(name):
    """Stdlib check that also works on Python 3.9 (no sys.stdlib_module_names): look at where the module lives."""
    spec = importlib.util.find_spec(name)
    if spec is None:
        return False                                  # not installed at all: treat as a third-party package
    origin = spec.origin or ""
    if origin in ("built-in", "frozen"):
        return True
    path = Path(origin).resolve()
    stdlib = Path(sysconfig.get_path("stdlib")).resolve()
    return stdlib in path.parents and not ({"site-packages", "dist-packages"} & set(path.parts))


def is_stdlib(name):
    names = getattr(sys, "stdlib_module_names", None)    # available from Python 3.10
    return name in names if names is not None else is_stdlib_by_location(name)


def third_party(modules):
    return {m for m in modules if not is_stdlib(m)}


@pytest.mark.parametrize("name", ["ast", "io", "shutil", "importlib", "types", "json", "os", "re", "argparse",
                                  "tempfile", "urllib", "http", "logging", "__future__"])
def test_stdlib_detection_fallback_recognises_standard_modules(name):
    assert is_stdlib_by_location(name), f"{name} should be recognised as standard library"


@pytest.mark.parametrize("name", ["pytest", "packaging", "not_an_installed_package_xyz"])
def test_stdlib_detection_fallback_does_not_treat_third_party_as_stdlib(name):
    assert not is_stdlib_by_location(name)


def test_runtime_imports_are_declared_or_knowingly_optional():
    external = third_party(top_level_imports(SCRIPT))
    assert external == {"youtube_transcript_api", "requests"}, f"unexpected third-party imports: {external}"
    declared = {re.split(r"[<>=!~\[ ]", ln, maxsplit=1)[0].lower() for ln in requirement_lines("requirements.txt")}
    assert "youtube-transcript-api" in declared
    # requests is imported only inside try/except ImportError (it ships with youtube-transcript-api).
    source = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"try:\n\s+import requests\n.*?except ImportError", source, re.S)


def test_nothing_unneeded_is_declared():
    declared = {re.split(r"[<>=!~\[ ]", ln, maxsplit=1)[0].lower() for ln in requirement_lines("requirements.txt")}
    assert declared == {"youtube-transcript-api"}


@pytest.mark.parametrize("name", ["requirements.txt", "requirements-dev.txt"])
def test_every_requirement_has_a_lower_and_upper_bound(name):
    for line in requirement_lines(name):
        if line.startswith("-r "):
            assert (ROOT / line[3:].strip()).is_file()
            continue
        assert re.search(r">=[^,]+,\s*<", line), f"{line!r} needs a compatible range like >=1.0,<2"


def test_dev_requirements_include_runtime_ones():
    assert "-r requirements.txt" in requirement_lines("requirements-dev.txt")


def test_tests_only_use_declared_third_party_packages():
    dev = {re.split(r"[<>=!~\[ ]", ln, maxsplit=1)[0].lower() for ln in requirement_lines("requirements-dev.txt")
           if not ln.startswith("-r")}
    used = set()
    for path in (ROOT / "tests").glob("*.py"):
        used |= third_party(top_level_imports(path))
    local = {"conftest", "transcript_under_test"}
    allowed = {"pytest", "youtube_transcript_api", "requests", "packaging"} | local
    assert used <= allowed, f"tests import undeclared packages: {used - allowed}"
    assert "pytest" in dev


def test_constraints_file_pins_are_exact_and_inside_the_declared_ranges():
    constraints = ROOT / "constraints.txt"
    assert constraints.is_file(), "constraints.txt (the reproducible-install pins) is missing"
    pytest.importorskip("packaging.requirements")
    from packaging.requirements import Requirement
    pins = {}
    for line in constraints.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        req = Requirement(line)                              # also validates the syntax, markers included
        specs = list(req.specifier)
        assert len(specs) == 1 and specs[0].operator == "==", f"{line!r} must be an exact == pin"
        pins.setdefault(req.name.lower(), []).append(specs[0].version)
    # Runtime dependencies and the linter must be pinned. pytest is intentionally left to its bounded range,
    # because the newest pytest differs between Python 3.9 and 3.13.
    must_pin = {"youtube-transcript-api", "ruff"}
    for line in requirement_lines("requirements.txt") + [ln for ln in requirement_lines("requirements-dev.txt")
                                                          if not ln.startswith("-r")]:
        req = Requirement(line)
        name = req.name.lower()
        if name in must_pin:
            assert name in pins, f"{req.name} is not pinned in constraints.txt"
        for version in pins.get(name, []):
            assert req.specifier.contains(version), f"pin {version} for {req.name} is outside {req.specifier}"


def test_constraints_pin_every_package_ci_installs_for_the_runtime_dependency():
    """youtube-transcript-api needs requests and defusedxml; their pins must exist so installs are reproducible."""
    text = (ROOT / "constraints.txt").read_text(encoding="utf-8").lower()
    for package in ("youtube-transcript-api", "requests", "defusedxml", "urllib3", "certifi", "idna",
                    "charset-normalizer"):
        assert re.search(rf"^{package}==", text, re.M), f"{package} is not pinned"
