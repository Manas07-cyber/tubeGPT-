#!/usr/bin/env python3
"""Enforce line AND branch coverage, function by function, for the security-critical code of transcript.py.

Usage: python .github/scripts/security_coverage.py coverage.json [--source path/to/transcript.py]

coverage.json comes from ``pytest --cov --cov-report=json:coverage.json`` with ``branch = True`` (see .coveragerc).
Whole-file coverage can hide an untested safety check, so every function that deletes, re-permissions, opens or
validates untrusted data is measured on its own. The run fails when

* a listed function is missing from the source, or is defined more than once (the report could not tell the
  definitions apart, so a gap in one could hide behind the other);
* a listed function has no measurable statements (a stub must not score 100%);
* a function's line or branch coverage is below the requirement of its risk tier (or of its documented exception);
* the report has no branch data (it was produced without ``branch = True``);
* a function that deletes or changes files or permissions is not listed at all, so new destructive code cannot
  slip past the gate unmeasured.

Functions are identified by qualified name (``outer.inner``, ``Class.method``), so nested functions and methods are
measured separately and two functions with the same plain name are never confused. Async functions are included.
A function's own lines exclude the bodies of functions and classes nested inside it, and exclude its ``def`` line
(which runs at import time and would otherwise count as covered even if the function never ran).
"""
import argparse
import ast
import json
import sys
from pathlib import Path
from typing import NamedTuple

SCRIPT = "skills/youtube-conversation/scripts/transcript.py"


class Req(NamedTuple):
    line: float    # minimum percentage of the function's executable lines that tests must run
    branch: float  # minimum percentage of its branch arcs (if/else, loops, try/except exits) that tests must take


# Requirements by risk. A tier is justified by what a defect in the function can cost, and the numbers by what CI
# measured on Linux / Python 3.13 (commit b4e73a7, 2026-11-11): every function listed below ran 100% of its lines;
# destructive functions took 100% of their measurable branches; the lowest boundary function (split_text) took
# 83.3% of its branches (the arcs left are the no-space-in-the-window split of unspaced text, run only by CJK input).
#   destructive: can delete or re-permission files, follow a link out of the store, or lose a transcript. A gap here
#                is data loss or a path-safety hole, so every error path must have been exercised: 95% of lines and
#                90% of branches leaves room for one defensive line the OS makes unreachable, and nothing more.
#   boundary:    parses or validates untrusted input, or builds error text. A gap here is a crash, a silently wrong
#                import or a leaked path, but nothing is deleted. Lines are held as high as destructive code; branches
#                at 80%, just under the lowest measured value, because some parser arcs exist only for rare inputs.
# These were first set to 90% / 80% before any measurement and only raised afterwards; never lower one to pass a run.
TIERS = {
    "destructive": Req(line=95, branch=90),
    "boundary": Req(line=95, branch=80),
}

# qualified name -> tier
CRITICAL = {
    # storage: folders, permissions, locks, atomic writes, cleanup, reads
    "default_dir": "destructive", "ensure_store": "destructive", "_tighten_dir": "destructive",
    "secure_store_files": "destructive", "_open_regular": "destructive", "marker_is_valid": "destructive",
    "_write_marker": "destructive", "store_file": "destructive", "store_lock": "destructive",
    "write_json_atomic": "destructive", "_replace": "destructive",
    "_out_may_replace": "destructive", "cleanup_store": "destructive",
    "cleanup_store.protected": "destructive", "cleanup_store.remove": "destructive",
    "_remove_if_unchanged": "destructive", "_scan_store": "destructive", "_dir_fd": "destructive",
    "cleanup_allowed": "destructive", "cmd_cleanup": "destructive", "save_doc": "destructive",
    "load_doc": "destructive", "_read_validated": "destructive", "_touch": "destructive",
    "storage_error": "destructive", "pasted_name": "destructive",
    # validation and input limits
    "validate_doc": "boundary", "_number": "boundary", "parse_time_arg": "boundary", "decode_input": "boundary",
    "read_stdin_bytes": "boundary", "read_input_text": "boundary", "split_text": "boundary",
    "enforce_segment_limit": "boundary", "segments_from_raw": "boundary", "make_api": "boundary",
    # parsing of untrusted text and identifiers
    "parse_cues": "boundary", "parse_panel": "boundary", "parse_panel.flush": "boundary", "parse_plain": "boundary",
    "parse_transcript_text": "boundary", "extract_video_id": "boundary", "clean_text": "boundary",
    # error reporting must not leak paths
    "scrub": "boundary", "classify_error": "boundary",
}

# Documented exceptions: a function that CI has measured below its tier and that cannot reasonably be raised.
# Each needs a reason; the numbers are the measured value minus a margin, and may only ever be raised.
EXCEPTIONS = {
    # qualname: (Req(line, branch), "why the tier cannot be met")
}

# Whole-file floors (from the report summary; the branch figure is covered_branches / num_branches).
# Measured at the same commit: 97.9% of lines and 95.3% of branches for the whole file.
OVERALL = Req(line=95, branch=90)

# Calls that delete, rename over, re-permission or create files. A function that makes one must be in CRITICAL.
_FS_MODULES = {"os", "shutil"}
_FS_OS_ATTRS = {"unlink", "remove", "rmdir", "rmtree", "replace", "rename", "chmod", "fchmod", "chown", "fchown",
                "truncate", "ftruncate", "symlink", "link", "utime", "mkdir", "makedirs", "mkstemp", "mkfifo"}
_FS_PATH_ATTRS = {"unlink", "rmdir", "write_text", "write_bytes", "touch", "symlink_to", "hardlink_to"}

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPES = _FUNCS + (ast.ClassDef,)


class Fn(NamedTuple):
    qualname: str
    kind: str            # "def", "async def"
    own: frozenset       # line numbers belonging to this function and not to something nested in it
    mutates_files: bool


def _own_lines(node):
    lines = set(range(node.body[0].lineno, node.end_lineno + 1))  # starts after the def line and decorators
    for child in ast.walk(node):
        if child is not node and isinstance(child, _SCOPES) and child.body:
            lines -= set(range(child.body[0].lineno, child.end_lineno + 1))
    return frozenset(lines)


def _calls_that_mutate(node):
    """True if this function's own statements (not nested functions) make a file-mutating call."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, _SCOPES):
            continue
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            attr, base = child.func.attr, child.func.value
            if isinstance(base, ast.Name) and base.id in _FS_MODULES and attr in _FS_OS_ATTRS:
                return True
            if attr in _FS_PATH_ATTRS:
                return True
        stack.extend(ast.iter_child_nodes(child))
    return False


def function_table(tree):
    """Map qualified name -> list of definitions (a list longer than one means an ambiguous name)."""
    table = {}

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _FUNCS):
                qualname = ".".join(scope + [child.name])
                kind = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                table.setdefault(qualname, []).append(Fn(qualname, kind, _own_lines(child), _calls_that_mutate(child)))
                visit(child, scope + [child.name])
            elif isinstance(child, ast.ClassDef):
                visit(child, scope + [child.name])
            else:
                visit(child, scope)

    visit(tree, [])
    return table


class Measure(NamedTuple):
    statements: int
    covered: int
    missing_lines: tuple
    branches: int
    branches_covered: int

    @property
    def line_pct(self):
        return 100.0 * self.covered / self.statements if self.statements else 0.0

    @property
    def branch_pct(self):
        return 100.0 * self.branches_covered / self.branches if self.branches else None


def measure(fn, entry):
    executed, missing = set(entry["executed_lines"]), set(entry["missing_lines"])
    ran, gap = fn.own & executed, fn.own & missing
    done = sum(1 for a, _ in entry["executed_branches"] if a in fn.own)
    undone = sum(1 for a, _ in entry["missing_branches"] if a in fn.own)
    return Measure(len(ran) + len(gap), len(ran), tuple(sorted(gap)), done + undone, done)


def requirement(qualname):
    if qualname in EXCEPTIONS:
        return EXCEPTIONS[qualname][0]
    return TIERS[CRITICAL[qualname]]


def check_config():
    """Problems with the policy itself (an exception for an unlisted function, a missing reason, a bad tier)."""
    problems = [f"{name}: unknown tier {tier!r}" for name, tier in CRITICAL.items() if tier not in TIERS]
    for name, (req, why) in EXCEPTIONS.items():
        if name not in CRITICAL:
            problems.append(f"{name}: has an exception but is not in CRITICAL")
        if not why.strip():
            problems.append(f"{name}: an exception needs a written reason")
        tier = TIERS.get(CRITICAL.get(name), None)
        if tier and (req.line > tier.line or req.branch > tier.branch):
            problems.append(f"{name}: an exception must be lower than its tier, otherwise it is not an exception")
    return problems


def analyse(source_text, entry):
    """Return (rows, failures). ``entry`` is the coverage report's record for the source file."""
    failures = check_config()
    for key in ("executed_branches", "missing_branches"):
        if key not in entry:
            failures.append(f"the coverage report has no '{key}': run the tests with branch coverage enabled")
            return [], failures
    table = function_table(ast.parse(source_text))
    rows = []
    for name in CRITICAL:
        definitions = table.get(name)
        if not definitions:
            failures.append(f"{name}: function not found in the source (renamed or deleted? update CRITICAL)")
            continue
        if len(definitions) > 1:
            failures.append(f"{name}: defined {len(definitions)} times, so its coverage cannot be told apart; "
                            "rename one of them")
            continue
        m = measure(definitions[0], entry)
        if m.statements == 0:
            failures.append(f"{name}: no measurable statements (an empty or stub function must not pass)")
            continue
        req = requirement(name)
        rows.append((name, m, req))
        if m.line_pct < req.line:
            failures.append(f"{name}: line coverage {m.line_pct:.1f}% is below {req.line}% "
                            f"(lines not run: {','.join(map(str, m.missing_lines)) or '-'})")
        if m.branch_pct is not None and m.branch_pct < req.branch:
            failures.append(f"{name}: branch coverage {m.branch_pct:.1f}% "
                            f"({m.branches_covered} of {m.branches}) is below {req.branch}%")
    for name, definitions in table.items():
        if any(d.mutates_files for d in definitions) and name not in CRITICAL:
            failures.append(f"{name}: deletes, renames or changes files or permissions but is not listed in "
                            "CRITICAL, so it is not covered by this gate")
    return rows, failures


def whole_file_failures(entry):
    summary = entry["summary"]
    statements, covered = summary["num_statements"], summary["covered_lines"]
    branches, branches_done = summary.get("num_branches", 0), summary.get("covered_branches", 0)
    line_pct = 100.0 * covered / statements if statements else 0.0
    branch_pct = 100.0 * branches_done / branches if branches else 0.0
    failures = []
    if line_pct < OVERALL.line:
        failures.append(f"whole file: line coverage {line_pct:.1f}% is below {OVERALL.line}%")
    if not branches:
        failures.append("whole file: the report contains no branch data")
    elif branch_pct < OVERALL.branch:
        failures.append(f"whole file: branch coverage {branch_pct:.1f}% is below {OVERALL.branch}%")
    return line_pct, branch_pct, statements, covered, branches, branches_done, failures


def find_entry(report, source):
    source = source.replace("\\", "/")  # a Windows path must match the report's forward-slash names too
    return next((v for k, v in report["files"].items() if k.replace("\\", "/").endswith(source)), None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("report", help="coverage.json")
    parser.add_argument("--source", default=SCRIPT, help="path of the measured source file")
    args = parser.parse_args(argv)
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    entry = find_entry(report, args.source)
    if entry is None:
        print(f"FAIL: {args.source} is not in the coverage report")
        return 1
    rows, failures = analyse(Path(args.source).read_text(encoding="utf-8"), entry)
    print(f"{'function':26} {'tier':11} {'lines':>7} {'need':>5}  {'branches':>16} {'need':>5}  lines not run")
    for name, m, req in sorted(rows, key=lambda r: (r[1].line_pct, r[0])):
        branch = "n/a (none)" if m.branch_pct is None else f"{m.branch_pct:5.1f}% {m.branches_covered}/{m.branches}"
        print(f"{name:26} {CRITICAL[name]:11} {m.line_pct:6.1f}% {req.line:4.0f}%  {branch:>16} {req.branch:4.0f}%  "
              f"{','.join(map(str, m.missing_lines)) or '-'}")
    if "summary" in entry:
        line_pct, branch_pct, stmts, covered, branches, done, extra = whole_file_failures(entry)
        print(f"\nwhole file: lines {line_pct:.1f}% ({covered}/{stmts}), "
              f"branches {branch_pct:.1f}% ({done}/{branches})")
        failures += extra
    for message in failures:
        print("FAIL:", message)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
