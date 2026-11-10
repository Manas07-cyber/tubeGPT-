"""Dependency declarations must match what the code really imports, with bounded versions."""
import ast
import os
import re
import sys
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


def is_stdlib(name):
    """True for any standard-library module of any platform (so Windows-only ``msvcrt`` counts on Linux too)."""
    return name in sys.stdlib_module_names           # exists on every supported Python (3.10 and newer)


def third_party(modules):
    return {m for m in modules if not is_stdlib(m)}


@pytest.mark.parametrize("name", ["ast", "io", "shutil", "importlib", "types", "json", "os", "re", "argparse",
                                  "tempfile", "urllib", "http", "logging", "__future__", "fcntl", "msvcrt"])
def test_standard_modules_of_every_platform_are_recognised(name):
    assert is_stdlib(name), f"{name} should be recognised as standard library"


@pytest.mark.parametrize("name", ["pytest", "packaging", "jsonschema", "not_an_installed_package_xyz"])
def test_third_party_modules_are_not_mistaken_for_standard_library(name):
    assert not is_stdlib(name)


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


def option_lines(name):
    return [ln for ln in requirement_lines(name) if ln.startswith("-")]


def package_lines(name):
    return [ln for ln in requirement_lines(name) if not ln.startswith("-")]


@pytest.mark.parametrize("name", ["requirements.txt", "requirements-dev.txt"])
def test_every_requirement_has_a_lower_and_upper_bound(name):
    for line in package_lines(name):
        assert re.search(r">=[^,]+,\s*<", line), f"{line!r} needs a compatible range like >=1.0,<2"
    for line in option_lines(name):
        assert re.match(r"-[rc] \S+$", line), f"unexpected option line {line!r}"
        assert (ROOT / line[3:].strip()).is_file(), f"{line!r} points at a missing file"


def test_dev_requirements_include_runtime_ones_and_apply_the_constraints():
    lines = requirement_lines("requirements-dev.txt")
    assert "-r requirements.txt" in lines and "-c constraints.txt" in lines


def test_tests_only_use_declared_third_party_packages():
    dev = {re.split(r"[<>=!~\[ ]", ln, maxsplit=1)[0].lower() for ln in package_lines("requirements-dev.txt")}
    used = set()
    for path in (ROOT / "tests").glob("*.py"):
        used |= third_party(top_level_imports(path))
    local = {"conftest", "transcript_under_test"}
    allowed = {"pytest", "youtube_transcript_api", "requests", "packaging", "jsonschema"} | local
    assert used <= allowed, f"tests import undeclared packages: {used - allowed}"
    assert {"pytest", "jsonschema", "pytest-cov", "ruff"} <= dev


def parse_pins(name):
    """{package: version} from a file of exact pins. Anything that is not 'name==version' is an error."""
    pins = {}
    for line in package_lines(name):
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][A-Za-z0-9.+!_-]*)", line)
        assert match, f"{name}: {line!r} is not an exact pin (name==version, no markers or ranges)"
        key = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        assert key not in pins, f"{name}: {match.group(1)} is pinned twice"
        pins[key] = match.group(2)
    return pins


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def test_constraints_pins_are_exact_and_inside_the_declared_ranges():
    pytest.importorskip("packaging.requirements")
    from packaging.requirements import Requirement
    pins = parse_pins("constraints.txt")
    for name in ("requirements.txt", "requirements-dev.txt"):
        for line in package_lines(name):
            req = Requirement(line)
            assert norm(req.name) in pins, f"{req.name} (from {name}) is not pinned in constraints.txt"
            assert req.specifier.contains(pins[norm(req.name)]), \
                f"pin {pins[norm(req.name)]} for {req.name} is outside {req.specifier}"


def test_constraints_pin_every_package_the_runtime_needs():
    """youtube-transcript-api needs requests and defusedxml; their dependencies must be pinned too."""
    pins = parse_pins("constraints.txt")
    for package in ("youtube-transcript-api", "requests", "defusedxml", "urllib3", "certifi", "idna",
                    "charset-normalizer", "pytest", "pytest-cov", "coverage", "jsonschema", "ruff"):
        assert package in pins, f"{package} is not pinned"


def test_ci_tooling_and_audit_tool_are_pinned_exactly():
    assert set(parse_pins("requirements-ci.txt")) == {"pip"}
    audit = parse_pins("requirements-audit.txt")
    assert "pip-audit" in audit and len(audit) > 5       # the scanner and everything it installs


@pytest.mark.skipif(os.environ.get("TUBEGPT_CHECK_INSTALLED_PINS") != "1",
                    reason="only meaningful in an environment installed from constraints.txt (CI sets "
                           "TUBEGPT_CHECK_INSTALLED_PINS=1); a developer's own environment may differ")
def test_the_installed_packages_are_exactly_the_pinned_ones():
    """Audits the set that really runs: every installed distribution is pinned, at the pinned version."""
    from importlib import metadata
    pins = parse_pins("constraints.txt")
    installed = {norm(d.metadata["Name"]): d.version for d in metadata.distributions()}
    installed.pop("pip", None)
    installed.pop("setuptools", None)                    # tooling of the environment itself, not a dependency
    missing = {name: version for name, version in installed.items() if name not in pins}
    wrong = {name: (version, pins[name]) for name, version in installed.items() if name in pins and pins[name] != version}
    assert not missing, f"installed but not pinned in constraints.txt: {missing}"
    assert not wrong, f"installed version differs from the pin (installed, pinned): {wrong}"
