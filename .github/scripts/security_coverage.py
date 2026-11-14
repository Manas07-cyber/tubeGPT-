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
    # Windows ownership and handle-based deletion: the decisions are tested on every platform with fake Win32
    # calls; the real calls are in WINDOWS_ONLY below.
    "_owned_by_me": "destructive", "_is_mine": "destructive", "_windows_owned_by_me": "destructive",
    "_pin_store": "destructive", "_delete_exact": "destructive",
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

# Thin wrappers over the Win32 API (through ctypes). They cannot run on the Linux job that measures coverage, so
# they carry ``# pragma: no cover`` and are exempt from the percentages. They are NOT exempt from scrutiny:
#   * every function whose name starts with ``_win_`` must be listed here (so new Win32 code cannot be added quietly);
#   * a listed function must be defined once, and ALL of its statements must be excluded by the pragma (a
#     function that has measurable statements belongs in CRITICAL, with a real requirement);
#   * their behaviour is verified only by the Windows CI jobs (tests/test_windows_security.py, ``on_windows``),
#     which this script does not read. A passing run of this script says nothing about them.
WINDOWS_ONLY = {
    "_win_api": "declares the Win32 functions used below (ctypes import lives here and nowhere else)",
    "_win_sid_text": "turns a SID into text",
    "_win_my_sids": "reads the SIDs of the current process token",
    "_win_owner_sid": "reads the owner SID of an open file or a path",
    "_win_fd": "wraps a Win32 handle in a C descriptor",
    "_win_open": "CreateFileW on an existing file or folder",
    "_win_open_for_delete": "opens a file (a link is not followed) with the right to delete it",
    "_win_pin_dir": "opens the store folder without delete-sharing so it cannot be renamed during cleanup",
    "_win_mark_delete": "requests deletion on an open handle (FileDispositionInfo)",
}

# Whole-file floors (from the report summary; the branch figure is covered_branches / num_branches).
# Measured at the same commit: 97.9% of lines and 95.3% of branches for the whole file.
OVERALL = Req(line=95, branch=90)

# --- What counts as "changes the file system" -----------------------------------------------------------------
# A function that does any of this must be listed in CRITICAL (or WINDOWS_ONLY below), so new destructive code
# cannot be added without a coverage requirement. The detector is a conservative static analysis of one source
# file; its limits are listed in LIMITATIONS and are tested. When it cannot tell, it reports "may change files".
_OS_MUTATORS = {
    "unlink", "remove", "rmdir", "removedirs", "rename", "renames", "replace", "chmod", "fchmod", "lchmod", "chown",
    "fchown", "lchown", "chflags", "lchflags", "truncate", "ftruncate", "symlink", "link", "utime", "mkdir",
    "makedirs", "mkfifo", "mknod", "write", "writev", "pwrite", "pwritev", "sendfile", "copy_file_range",
    "system", "popen", "startfile", "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn",
    "posix_spawnp", "fork", "forkpty"}
_SHUTIL_MUTATORS = {"rmtree", "move", "copy", "copy2", "copyfile", "copyfileobj", "copymode", "copystat", "copytree",
                    "chown", "make_archive", "unpack_archive"}
_TEMPFILE_MUTATORS = {"mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryFile", "TemporaryDirectory",
                      "SpooledTemporaryFile"}
_PATH_CLASSES = ("pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath", "pathlib.WindowsPath")
_PATH_MUTATORS = {"unlink", "rmdir", "write_text", "write_bytes", "touch", "symlink_to", "hardlink_to", "link_to",
                  "mkdir", "rename", "chmod", "lchmod", "extract", "extractall"}
# Calls whose effect depends on a mode argument (read-only modes are not changes). The number is the mode's
# positional index; a mode that is not a plain string constant is treated as writing.
_OPEN_CALLS = {"builtins.open": 1, "io.open": 1, "codecs.open": 1, "os.fdopen": 1, "gzip.open": 1, "bz2.open": 1,
               "lzma.open": 1}
_ALWAYS_MUTATING = {"zipfile.ZipFile", "tarfile.open", "tarfile.TarFile", "sqlite3.connect", "subprocess.run",
                    "subprocess.call", "subprocess.check_call", "subprocess.check_output", "subprocess.Popen",
                    "subprocess.getoutput", "subprocess.getstatusoutput", "ctypes.windll", "ctypes.WinDLL"}
_OS_OPEN_READ_FLAGS = {"O_RDONLY", "O_NOFOLLOW", "O_CLOEXEC", "O_BINARY", "O_DIRECTORY", "O_NONBLOCK", "O_NOCTTY",
                       "O_NOINHERIT", "O_NOATIME", "O_PATH", "O_SEQUENTIAL", "O_RANDOM"}
# Win32 entry points that create, delete, move or re-permission files. They are called through ctypes handles, so
# they are recognised by name wherever they are referenced.
_WIN32_MUTATORS = {
    "CreateFileW", "CreateFileA", "DeleteFileW", "DeleteFileA", "MoveFileW", "MoveFileExW", "ReplaceFileW", "CopyFileW",
    "CopyFileExW", "SetFileInformationByHandle", "SetFileAttributesW", "SetFileTime", "SetEndOfFile", "WriteFile",
    "SetNamedSecurityInfoW", "SetSecurityInfo", "RemoveDirectoryW", "CreateDirectoryW", "CreateSymbolicLinkW",
    "CreateHardLinkW", "DeviceIoControl"}
_DYNAMIC = {"__import__", "eval", "exec", "compile", "importlib.import_module", "builtins.__import__",
            "builtins.eval", "builtins.exec", "builtins.compile"}
_TRACKED_MODULES = {"os", "shutil", "tempfile", "pathlib", "io", "codecs", "gzip", "bz2", "lzma", "zipfile",
                    "tarfile", "sqlite3", "subprocess", "ctypes", "builtins", "importlib", "posix", "nt"}

LIMITATIONS = """What this detector can and cannot see (all of it is a static, per-file analysis):
  seen:     calls and references (aliases, ``from x import y as z``, ``z = os.unlink``, callbacks such as
            ``map(os.remove, ...)``) to os / shutil / tempfile / pathlib / io / zipfile / tarfile / sqlite3 /
            subprocess / ctypes-Win32 mutators; open()/os.open() with a writing or unknowable mode or flags;
            Path-style methods on any object (``x.unlink()``, ``x.replace(target)``); getattr/__import__/eval on
            those modules; changes made while the module or a class body runs; nested and async functions.
  not seen: a changing call hidden behind another module's function (a helper imported from elsewhere), objects
            passed in from outside, writes through an already open file object (``handle.write``), third-party
            libraries, C extensions, and dynamic dispatch the analysis cannot resolve (it reports the unresolvable
            forms it recognises, but cannot find every one). A function that only CALLS a flagged function is not
            flagged itself; the flagged function must be gated.
  So a pass means "no known pattern was missed", never "no function can change files"."""

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPES = _FUNCS + (ast.ClassDef,)


class Fn(NamedTuple):
    qualname: str
    kind: str            # "def", "async def"
    own: frozenset       # line numbers belonging to this function and not to something nested in it
    reasons: tuple       # why the function may change files (empty if it does not)

    @property
    def mutates_files(self):
        return bool(self.reasons)


def _own_lines(node):
    lines = set(range(node.body[0].lineno, node.end_lineno + 1))  # starts after the def line and decorators
    for child in ast.walk(node):
        if child is not node and isinstance(child, _SCOPES) and child.body:
            lines -= set(range(child.body[0].lineno, child.end_lineno + 1))
    return frozenset(lines)


def _own_nodes(body):
    """Every node of these statements that belongs to this scope (nested functions and classes are separate)."""
    stack = list(body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _SCOPES):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _resolve(expr, env):
    """Dotted origin of a name or attribute chain (``o.unlink`` -> ``os.unlink``), or None if unknown."""
    if isinstance(expr, ast.Name):
        return env.get(expr.id)
    if isinstance(expr, ast.Attribute):
        base = _resolve(expr.value, env)
        return f"{base}.{expr.attr}" if base else None
    return None


def _bind(body, env):
    """Add the import aliases and simple assignment aliases of these statements to ``env`` (flow-insensitive)."""
    nodes = list(_own_nodes(body))
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                env[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                if alias.name != "*":
                    env[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    for _ in range(3):  # aliases of aliases
        for node in nodes:
            target = value = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            if isinstance(target, ast.Name):
                origin = _resolve(value, env)
                if origin and origin.split(".")[0] in _TRACKED_MODULES:
                    env[target.id] = origin
    return env


def _star_imports(tree):
    return sorted({n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
                   and any(a.name == "*" for a in n.names) and n.module.split(".")[0] in _TRACKED_MODULES})


def _mode_writes(call, index):
    node = next((k.value for k in call.keywords if k.arg == "mode"), None)
    if node is None and len(call.args) > index:
        node = call.args[index]
    if node is None:
        return False  # the default mode is "r"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return any(c in node.value for c in "wax+")
    return True       # not a constant: cannot be shown to be read-only


def _flag_is_read_only(node):
    if isinstance(node, ast.BinOp):
        return isinstance(node.op, ast.BitOr) and _flag_is_read_only(node.left) and _flag_is_read_only(node.right)
    if isinstance(node, ast.Name):
        return node.id.lstrip("_") in _OS_OPEN_READ_FLAGS
    if isinstance(node, ast.Attribute):
        return node.attr in _OS_OPEN_READ_FLAGS
    if isinstance(node, ast.Constant):
        return node.value == 0
    if isinstance(node, ast.Call):  # getattr(os, "O_NOFOLLOW", 0)
        return (isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant) and node.args[1].value in _OS_OPEN_READ_FLAGS)
    return False


def _flags_read_only(call):
    node = next((k.value for k in call.keywords if k.arg == "flags"), None)
    if node is None and len(call.args) > 1:
        node = call.args[1]
    return node is not None and _flag_is_read_only(node)  # a variable or unknown constant cannot be shown read-only


def _reason_for_reference(origin):
    module, _, attr = origin.rpartition(".")
    if module == "os" and attr in _OS_MUTATORS:
        return origin
    if module == "shutil" and attr in _SHUTIL_MUTATORS:
        return origin
    if module == "tempfile" and attr in _TEMPFILE_MUTATORS:
        return origin
    if module in _PATH_CLASSES and attr in _PATH_MUTATORS | {"replace"}:
        return origin
    if origin in _ALWAYS_MUTATING:
        return origin
    return None


def _mutation_reasons(body, module_env, params=()):
    """Why the statements in ``body`` may change files: a sorted tuple of short descriptions (empty if they cannot)."""
    env = {k: v for k, v in module_env.items() if k not in params}
    _bind(body, env)
    reasons = set()
    nodes = list(_own_nodes(body))
    # ``os.unlink in os.supports_dir_fd`` only asks a question about a function; it does not use it.
    tests = [n for n in nodes if isinstance(n, ast.Compare) and all(isinstance(o, (ast.In, ast.NotIn)) for o in n.ops)]
    asked = {id(part) for n in tests for part in [n.left, *n.comparators]}
    for node in nodes:
        if isinstance(node, ast.Attribute) and node.attr in _WIN32_MUTATORS:
            reasons.add(f"Win32 {node.attr}")
        if id(node) in asked:
            continue
        if isinstance(node, (ast.Attribute, ast.Name)) and isinstance(getattr(node, "ctx", None), ast.Load):
            origin = _resolve(node, env)
            if origin:
                found = _reason_for_reference(origin)
                if found:
                    reasons.add(found)
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        origin = _resolve(func, env)
        name = func.id if isinstance(func, ast.Name) else None
        to_devnull = bool(node.args) and _resolve(node.args[0], env) == "os.devnull"  # discarding output is no change
        if origin is None and name == "open" and "open" not in env and not to_devnull and _mode_writes(node, 1):
            reasons.add("open() for writing")
        if origin in _OPEN_CALLS and not to_devnull and _mode_writes(node, _OPEN_CALLS[origin]):
            reasons.add(f"{origin}() for writing")
        if origin == "os.open" and not _flags_read_only(node):
            reasons.add("os.open() that may create or write")
        if origin in _DYNAMIC or (origin is None and name in {"__import__", "eval", "exec", "compile"}):
            reasons.add("dynamic import or code execution")
        if name == "getattr" and node.args and (_resolve(node.args[0], env) or "").split(".")[0] in _TRACKED_MODULES:
            attr = node.args[1] if len(node.args) > 1 else None
            if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                found = _reason_for_reference(f"{_resolve(node.args[0], env)}.{attr.value}")
                if found:
                    reasons.add(f"getattr -> {found}")
            else:
                reasons.add("getattr on a file-system module with a computed name")
        if isinstance(func, ast.Attribute) and origin is None:  # a method on an object whose type is not known
            if func.attr in _PATH_MUTATORS:
                reasons.add(f".{func.attr}()")
            elif func.attr == "replace" and len(node.args) + len(node.keywords) == 1:
                reasons.add(".replace(target) (Path.replace)")  # str.replace always has two arguments
            elif func.attr == "open" and _mode_writes(node, 0):
                reasons.add(".open() for writing")
    return tuple(sorted(reasons))


def _module_env(tree):
    env = {}
    _bind(tree.body, env)
    return {k: v for k, v in env.items() if v.split(".")[0] in _TRACKED_MODULES}


def _calls_that_mutate(node, module_env=None):
    """True if this function's own statements (not nested functions) may change files. Kept for callers and tests."""
    return bool(_mutation_reasons(node.body, module_env or {}, _params(node)))


def _params(node):
    a = node.args
    return {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs} | {x.arg for x in (a.vararg, a.kwarg) if x}


def import_time_mutations(tree):
    """Changes made while the module or a class body is executed (not inside any function): never allowed."""
    env, found = _module_env(tree), []
    found += [("<module>", r) for r in _mutation_reasons(tree.body, env)]
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            found += [(node.name, r) for r in _mutation_reasons(node.body, env)]
    return found


def function_table(tree):
    """Map qualified name -> list of definitions (a list longer than one means an ambiguous name)."""
    table = {}
    env = _module_env(tree)

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _FUNCS):
                qualname = ".".join(scope + [child.name])
                kind = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                reasons = _mutation_reasons(child.body, env, _params(child))
                table.setdefault(qualname, []).append(Fn(qualname, kind, _own_lines(child), reasons))
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
    tree = ast.parse(source_text)
    table = function_table(tree)
    failures += [f"'from {module} import *' hides which functions are called, so this file cannot be analysed"
                 for module in _star_imports(tree)]
    failures += [f"{where}: changes files when the module is imported ({reason}); never allowed"
                 for where, reason in import_time_mutations(tree)]
    for name in table:
        if name.split(".")[-1].startswith("_win_") and name not in WINDOWS_ONLY:
            failures.append(f"{name}: a Win32 helper (name starts with _win_) that is not listed in WINDOWS_ONLY")
    for name in WINDOWS_ONLY:
        definitions = table.get(name)
        if not definitions:
            failures.append(f"{name}: listed in WINDOWS_ONLY but not found in the source")
        elif len(definitions) > 1:
            failures.append(f"{name}: defined {len(definitions)} times; a Windows-only function must be unambiguous")
        elif "excluded_lines" not in entry:
            failures.append(f"{name}: the report has no 'excluded_lines', so the pragma cannot be checked")
        elif measure(definitions[0], entry).statements:
            failures.append(f"{name}: has statements that were measured, so it is not excluded as Windows-only: "
                            "give it a CRITICAL tier instead (or mark every statement 'pragma: no cover')")
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
        if name in CRITICAL or name in WINDOWS_ONLY:
            continue
        reasons = sorted({r for d in definitions for r in d.reasons})
        if reasons:
            failures.append(f"{name}: may change files, permissions or processes ({', '.join(reasons)}) but is not "
                            "listed in CRITICAL, so it is not covered by this gate")
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
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], epilog=LIMITATIONS,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
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
