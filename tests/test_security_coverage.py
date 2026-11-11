"""Unit tests for .github/scripts/security_coverage.py, the per-function line and branch coverage gate.

The gate protects the storage and parsing code, so it must not be gameable by accident: a missing, duplicated,
nested, async or empty function must never score a free pass. The tests build small source files and matching
coverage reports, so each rule is checked on its own.
"""
import ast
import importlib.util
import json
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / ".github" / "scripts" / "security_coverage.py"
REAL_SOURCE = ROOT / "skills" / "youtube-conversation" / "scripts" / "transcript.py"


@pytest.fixture
def gate(monkeypatch):
    spec = importlib.util.spec_from_file_location("security_coverage_under_test", GATE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["security_coverage_under_test"] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "EXCEPTIONS", {})
    monkeypatch.setattr(mod, "OVERALL", mod.Req(0, 0))
    return mod


def statement_lines(source):
    return {n.lineno for n in ast.walk(ast.parse(source)) if isinstance(n, ast.stmt)}


def report(gate, source, *, run=(), skip=(), arcs_done=(), arcs_missing=()):
    """A coverage entry. ``run`` / ``skip`` are qualified names whose own statements were / were not executed."""
    table = gate.function_table(ast.parse(source))
    stmts = statement_lines(source)
    executed, missing = set(), set()
    for name in run:
        executed |= set(table[name][0].own) & stmts
    for name in skip:
        missing |= set(table[name][0].own) & stmts
    return {"executed_lines": sorted(executed), "missing_lines": sorted(missing),
            "executed_branches": [list(a) for a in arcs_done], "missing_branches": [list(a) for a in arcs_missing],
            "summary": {"num_statements": len(executed) + len(missing), "covered_lines": len(executed),
                        "num_branches": len(arcs_done) + len(arcs_missing), "covered_branches": len(arcs_done)}}


def first_line_of(source, name):
    return next(n.body[0].lineno for n in ast.walk(ast.parse(source))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


SIMPLE = textwrap.dedent('''
    def guard(path):
        if path.startswith("/"):
            return False
        return True
''')


def use(gate, monkeypatch, **critical):
    monkeypatch.setattr(gate, "CRITICAL", critical)


# ------------------------------------------------------------------------------------ the basic verdicts
def test_a_fully_covered_function_passes(gate, monkeypatch):
    use(gate, monkeypatch, guard="boundary")
    rows, failures = gate.analyse(SIMPLE, report(gate, SIMPLE, run=["guard"]))
    assert failures == [] and rows[0][1].line_pct == 100.0


def test_a_listed_function_that_does_not_exist_fails(gate, monkeypatch):
    use(gate, monkeypatch, renamed_function="boundary")
    _, failures = gate.analyse(SIMPLE, report(gate, SIMPLE, run=["guard"]))
    assert any("renamed_function" in f and "not found" in f for f in failures)


def test_line_coverage_below_the_tier_fails_and_names_the_missing_lines(gate, monkeypatch):
    use(gate, monkeypatch, guard="boundary")
    entry = report(gate, SIMPLE, run=[], skip=["guard"])
    _, failures = gate.analyse(SIMPLE, entry)
    assert any("guard: line coverage 0.0%" in f and "lines not run" in f for f in failures)


def test_partial_line_coverage_is_measured_not_rounded_up(gate, monkeypatch):
    use(gate, monkeypatch, guard="boundary")
    entry = report(gate, SIMPLE, run=["guard"])
    last = max(entry["executed_lines"])
    entry["executed_lines"].remove(last)       # one of three statements was not run: 66.7%, not rounded up to 90
    entry["missing_lines"].append(last)
    rows, failures = gate.analyse(SIMPLE, entry)
    assert round(rows[0][1].line_pct, 1) == 66.7 and any("66.7%" in f for f in failures)


# ------------------------------------------------------------------------------------ branches are enforced
def test_branch_coverage_below_the_tier_fails_even_when_every_line_ran(gate, monkeypatch):
    use(gate, monkeypatch, guard="boundary")
    first = first_line_of(SIMPLE, "guard")
    entry = report(gate, SIMPLE, run=["guard"], arcs_done=[(first, first + 1)], arcs_missing=[(first, first + 2)])
    rows, failures = gate.analyse(SIMPLE, entry)
    assert rows[0][1].line_pct == 100.0 and rows[0][1].branch_pct == 50.0
    assert any("branch coverage 50.0% (1 of 2)" in f for f in failures)


def test_all_branches_taken_passes(gate, monkeypatch):
    use(gate, monkeypatch, guard="boundary")
    first = first_line_of(SIMPLE, "guard")
    entry = report(gate, SIMPLE, run=["guard"], arcs_done=[(first, first + 1), (first, first + 2)])
    assert gate.analyse(SIMPLE, entry)[1] == []


def test_a_function_without_branches_is_not_failed_for_branch_coverage(gate, monkeypatch):
    source = "def plain(x):\n    y = x + 1\n    return y\n"
    use(gate, monkeypatch, plain="boundary")
    rows, failures = gate.analyse(source, report(gate, source, run=["plain"]))
    assert failures == [] and rows[0][1].branch_pct is None


def test_a_report_without_branch_data_fails_loudly(gate, monkeypatch):
    use(gate, monkeypatch, guard="boundary")
    entry = report(gate, SIMPLE, run=["guard"])
    del entry["executed_branches"], entry["missing_branches"]
    _, failures = gate.analyse(SIMPLE, entry)
    assert any("branch coverage enabled" in f for f in failures)


def test_branches_of_other_functions_are_not_attributed_to_this_one(gate, monkeypatch):
    source = SIMPLE + "\n\ndef other(x):\n    while x:\n        x -= 1\n    return x\n"
    use(gate, monkeypatch, guard="boundary")
    other_first = first_line_of(source, "other")
    entry = report(gate, source, run=["guard"], arcs_missing=[(other_first, other_first + 1)])
    assert gate.analyse(source, entry)[1] == []


# ------------------------------------------------------------------------------------ no free passes
def test_a_function_with_no_measurable_statements_fails_instead_of_scoring_100(gate, monkeypatch):
    source = 'def stub():\n    """Only a docstring."""\n'
    use(gate, monkeypatch, stub="boundary")
    _, failures = gate.analyse(source, report(gate, source))
    assert any("stub" in f and "no measurable statements" in f for f in failures)


def test_a_function_missing_from_the_report_lines_fails_as_unmeasurable(gate, monkeypatch):
    use(gate, monkeypatch, guard="boundary")
    entry = report(gate, SIMPLE)     # the report knows nothing about the function's lines
    assert any("no measurable statements" in f for f in gate.analyse(SIMPLE, entry)[1])


def test_the_def_line_alone_does_not_count_as_coverage(gate, monkeypatch):
    """The def line runs at import time; it must not make a never-called function look covered."""
    use(gate, monkeypatch, guard="boundary")
    entry = report(gate, SIMPLE, skip=["guard"])
    def_line = ast.parse(SIMPLE).body[0].lineno
    entry["executed_lines"].append(def_line)
    rows, failures = gate.analyse(SIMPLE, entry)
    assert rows[0][1].covered == 0 and failures


# ------------------------------------------------------------------------------------ identification
def test_two_functions_with_the_same_name_are_reported_as_ambiguous(gate, monkeypatch):
    source = SIMPLE + "\n\nif True:\n    def guard(path):\n        return None\n"
    use(gate, monkeypatch, guard="boundary")
    entry = {"executed_lines": sorted(statement_lines(source)), "missing_lines": [], "executed_branches": [],
             "missing_branches": [], "summary": {"num_statements": 1, "covered_lines": 1}}
    _, failures = gate.analyse(source, entry)
    assert any("guard: defined 2 times" in f for f in failures)


def test_the_same_method_name_in_two_classes_is_never_confused(gate, monkeypatch):
    source = textwrap.dedent('''
        class A:
            def read(self):
                return 1

        class B:
            def read(self):
                x = 1
                return x
    ''')
    use(gate, monkeypatch, **{"A.read": "boundary", "B.read": "boundary"})
    entry = report(gate, source, run=["A.read"], skip=["B.read"])
    _, failures = gate.analyse(source, entry)
    assert len(failures) == 1 and failures[0].startswith("B.read")


def test_a_bare_method_name_does_not_match_a_method(gate, monkeypatch):
    source = "class A:\n    def read(self):\n        return 1\n"
    use(gate, monkeypatch, read="boundary")
    assert any("read: function not found" in f for f in gate.analyse(source, report(gate, source))[1])


NESTED = textwrap.dedent('''
    def outer(items):
        def helper(x):
            if x:
                return 1
            return 2
        total = 0
        for item in items:
            total += helper(item)
        return total
''')


def test_nested_functions_are_measured_separately_from_their_parent(gate, monkeypatch):
    use(gate, monkeypatch, **{"outer": "boundary", "outer.helper": "boundary"})
    entry = report(gate, NESTED, run=["outer"], skip=["outer.helper"])   # the closure was never executed
    rows, failures = gate.analyse(NESTED, entry)
    by_name = {name: m for name, m, _ in rows}
    assert by_name["outer"].line_pct == 100.0, "the closure's lines must not count against (or for) the parent"
    assert by_name["outer.helper"].line_pct == 0.0
    assert len(failures) == 1 and failures[0].startswith("outer.helper")


def test_a_parent_cannot_hide_behind_its_covered_nested_function(gate, monkeypatch):
    use(gate, monkeypatch, **{"outer": "boundary", "outer.helper": "boundary"})
    entry = report(gate, NESTED, run=["outer.helper"], skip=["outer"])
    _, failures = gate.analyse(NESTED, entry)
    assert len(failures) == 1 and failures[0].startswith("outer: line coverage 0.0%")


def test_the_defining_line_of_a_nested_function_belongs_to_its_parent(gate):
    table = gate.function_table(ast.parse(NESTED))
    helper_def = next(n.lineno for n in ast.walk(ast.parse(NESTED))
                      if isinstance(n, ast.FunctionDef) and n.name == "helper")
    assert helper_def in table["outer"][0].own and helper_def not in table["outer.helper"][0].own


ASYNC = textwrap.dedent('''
    async def fetch(url):
        if not url:
            return None
        return url
''')


def test_async_functions_are_found_and_measured(gate, monkeypatch):
    use(gate, monkeypatch, fetch="boundary")
    rows, failures = gate.analyse(ASYNC, report(gate, ASYNC, run=["fetch"]))
    assert failures == [] and gate.function_table(ast.parse(ASYNC))["fetch"][0].kind == "async def"
    _, failures = gate.analyse(ASYNC, report(gate, ASYNC, skip=["fetch"]))
    assert failures


def test_a_nested_async_function_inside_a_method_has_a_full_qualified_name(gate):
    source = "class C:\n    def m(self):\n        async def inner():\n            return 1\n        return inner\n"
    assert set(gate.function_table(ast.parse(source))) == {"C.m", "C.m.inner"}


# ------------------------------------------------------------------------------------ unlisted destructive code
DESTRUCTIVE = textwrap.dedent('''
    import os
    from pathlib import Path

    def wipe(path):
        os.unlink(path)

    def harmless(path):
        return path.upper()

    def writes(path):
        Path(path).write_text("x")

    def outer():
        def inner(path):
            os.remove(path)
        return inner
''')


def test_a_function_that_deletes_files_must_be_listed(gate, monkeypatch):
    use(gate, monkeypatch, harmless="boundary")
    entry = report(gate, DESTRUCTIVE, run=["harmless"])
    _, failures = gate.analyse(DESTRUCTIVE, entry)
    unlisted = {f.split(":")[0] for f in failures if "not listed" in f}
    assert unlisted == {"wipe", "writes", "outer.inner"}, "harmless code needs no listing; the parent of a closure neither"


def test_listing_a_destructive_function_satisfies_the_rule(gate, monkeypatch):
    use(gate, monkeypatch, wipe="destructive", writes="destructive", **{"outer.inner": "destructive"})
    entry = report(gate, DESTRUCTIVE, run=["wipe", "writes", "outer.inner"])
    assert gate.analyse(DESTRUCTIVE, entry)[1] == []


def test_string_replace_is_not_mistaken_for_a_file_operation(gate):
    source = "def clean(text):\n    return text.replace('a', 'b').remove if False else text.replace('a', 'b')\n"
    assert gate.function_table(ast.parse(source))["clean"][0].mutates_files is False


# ------------------------------------------------------------------------------------ policy and exceptions
def test_an_exception_lowers_the_requirement_for_that_function_only(gate, monkeypatch):
    use(gate, monkeypatch, guard="boundary")
    monkeypatch.setattr(gate, "EXCEPTIONS", {"guard": (gate.Req(60, 0), "the unreachable error path is a defence")})
    entry = report(gate, SIMPLE, run=["guard"])
    last = max(entry["executed_lines"])
    entry["executed_lines"].remove(last)
    entry["missing_lines"].append(last)           # 66.7%: fails the tier (90) but meets the exception (60)
    assert gate.analyse(SIMPLE, entry)[1] == []


@pytest.mark.parametrize("exceptions, expect", [
    ({"unlisted": (None, "why")}, "not in CRITICAL"),
    ({"guard": ("req", "")}, "needs a written reason"),
])
def test_invalid_exceptions_fail_the_gate(gate, monkeypatch, exceptions, expect):
    use(gate, monkeypatch, guard="boundary")
    fixed = {k: (gate.Req(10, 10) if v[0] else gate.Req(10, 10), v[1]) for k, v in exceptions.items()}
    monkeypatch.setattr(gate, "EXCEPTIONS", fixed)
    assert any(expect in p for p in gate.check_config())


def test_an_exception_that_is_not_lower_than_the_tier_is_rejected(gate, monkeypatch):
    use(gate, monkeypatch, guard="boundary")
    monkeypatch.setattr(gate, "EXCEPTIONS", {"guard": (gate.Req(100, 100), "stricter than the tier")})
    assert any("must be lower" in p for p in gate.check_config())


def test_an_unknown_tier_is_rejected(gate, monkeypatch):
    use(gate, monkeypatch, guard="nonexistent")
    assert any("unknown tier" in p for p in gate.check_config())


def test_the_real_policy_is_internally_consistent():
    spec = importlib.util.spec_from_file_location("security_coverage_real_policy", GATE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["security_coverage_real_policy"] = mod
    spec.loader.exec_module(mod)
    assert mod.check_config() == []
    assert all(t in mod.TIERS for t in mod.CRITICAL.values())


# ------------------------------------------------------------------------------------ whole-file floors
def test_whole_file_floors_are_enforced_for_lines_and_branches(gate, monkeypatch):
    monkeypatch.setattr(gate, "OVERALL", gate.Req(90, 80))
    entry = {"summary": {"num_statements": 100, "covered_lines": 85, "num_branches": 10, "covered_branches": 9}}
    *_, failures = gate.whole_file_failures(entry)
    assert failures == ["whole file: line coverage 85.0% is below 90%"]
    entry["summary"].update(covered_lines=95, covered_branches=7)
    *_, failures = gate.whole_file_failures(entry)
    assert failures == ["whole file: branch coverage 70.0% is below 80%"]
    entry["summary"].update(num_branches=0, covered_branches=0)
    *_, failures = gate.whole_file_failures(entry)
    assert failures == ["whole file: the report contains no branch data"]


# ------------------------------------------------------------------------------------ the command line
def write_case(tmp_path, source, entry_kwargs, gate):
    src = tmp_path / "sample.py"
    src.write_text(source, encoding="utf-8")
    entry = report(gate, source, **entry_kwargs)
    data = {"files": {str(src): entry}}
    cov = tmp_path / "coverage.json"
    cov.write_text(json.dumps(data), encoding="utf-8")
    return src, cov


def test_the_command_line_exit_codes(gate, monkeypatch, tmp_path, capsys):
    use(gate, monkeypatch, guard="boundary")
    first = first_line_of(SIMPLE, "guard")
    both_taken = [(first, first + 1), (first, first + 2)]
    src, cov = write_case(tmp_path, SIMPLE, {"run": ["guard"], "arcs_done": both_taken}, gate)
    assert gate.main([str(cov), "--source", str(src)]) == 0
    assert "guard" in capsys.readouterr().out
    src, cov = write_case(tmp_path, SIMPLE, {"skip": ["guard"], "arcs_missing": both_taken}, gate)
    assert gate.main([str(cov), "--source", str(src)]) == 1
    assert "FAIL: guard: line coverage" in capsys.readouterr().out


def test_a_report_that_does_not_contain_the_source_file_fails(gate, monkeypatch, tmp_path, capsys):
    use(gate, monkeypatch, guard="boundary")
    src, cov = write_case(tmp_path, SIMPLE, {"run": ["guard"]}, gate)
    assert gate.main([str(cov), "--source", "skills/not-in-the-report.py"]) == 1
    assert "not in the coverage report" in capsys.readouterr().out


# ------------------------------------------------------------------------------------ the real script
def test_every_listed_function_exists_once_has_statements_and_no_destructive_function_is_unlisted():
    """Run the real policy against the real source with a synthetic 'everything ran' report."""
    spec = importlib.util.spec_from_file_location("security_coverage_real", GATE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["security_coverage_real"] = mod
    spec.loader.exec_module(mod)
    source = REAL_SOURCE.read_text(encoding="utf-8")
    stmts = statement_lines(source)
    executed = sorted(stmts)
    entry = {"executed_lines": executed, "missing_lines": [], "executed_branches": [], "missing_branches": [],
             "summary": {"num_statements": len(executed), "covered_lines": len(executed)}}
    rows, failures = mod.analyse(source, entry)
    assert failures == [], failures
    assert len(rows) == len(mod.CRITICAL)
