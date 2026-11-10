#!/usr/bin/env python3
"""Report, and enforce, line coverage of the security-critical functions of transcript.py.

Usage: python .github/scripts/security_coverage.py coverage.json

coverage.json comes from ``pytest --cov --cov-report=json:coverage.json``. Overall coverage can hide an untested
safety check, so each function that guards storage, input limits or parsing is measured on its own. A function
below its floor, or missing from the report, fails the run. The floors only ever go up.
"""
import ast
import json
import sys
from pathlib import Path

SCRIPT = "skills/youtube-conversation/scripts/transcript.py"
OVERALL_FLOOR = 0
DEFAULT_FLOOR = 0
# function name -> minimum percentage of its executable lines that tests must run
FLOORS = {}
CRITICAL = [
    # storage
    "ensure_store", "_tighten_dir", "secure_store_files", "_open_regular", "marker_is_valid", "_write_marker",
    "store_file", "store_lock", "write_json_atomic", "cleanup_store", "_remove_if_unchanged", "_scan_store",
    "cleanup_allowed", "save_doc", "load_doc", "_read_validated", "storage_error", "pasted_name",
    # validation and input limits
    "validate_doc", "_number", "parse_time_arg", "decode_input", "read_stdin_bytes", "read_input_text",
    "split_text", "enforce_segment_limit", "segments_from_raw",
    # parsing of untrusted text and identifiers
    "parse_cues", "parse_panel", "parse_plain", "parse_transcript_text", "extract_video_id", "clean_text",
    # error reporting must not leak paths
    "scrub", "classify_error",
]


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    entry = next((v for k, v in report["files"].items() if k.replace("\\", "/").endswith(SCRIPT)), None)
    if entry is None:
        print(f"{SCRIPT} is not in the coverage report")
        return 1
    executed, missing = set(entry["executed_lines"]), set(entry["missing_lines"])
    tree = ast.parse(Path(SCRIPT).read_text(encoding="utf-8"))
    spans = {n.name: (n.lineno, n.end_lineno) for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    failures, rows = [], []
    for name in CRITICAL:
        if name not in spans:
            failures.append(f"{name}: function not found in {SCRIPT}")
            continue
        lo, hi = spans[name]
        ran = {n for n in executed if lo <= n <= hi}
        gap = {n for n in missing if lo <= n <= hi}
        total = len(ran) + len(gap)
        pct = 100.0 * len(ran) / total if total else 100.0
        floor = FLOORS.get(name, DEFAULT_FLOOR)
        rows.append((pct, name, sorted(gap), floor))
        if pct < floor:
            failures.append(f"{name}: {pct:.1f}% is below its floor of {floor}%")
    print(f"{'function':28} {'covered':>8}  floor  lines not run")
    for pct, name, gap, floor in sorted(rows):
        lines = ",".join(map(str, gap)) if gap else "-"
        print(f"{name:28} {pct:7.1f}%  {floor:4}%  {lines}")
    summary = entry["summary"]
    overall = summary["percent_covered"]
    print(f"\nwhole file: {overall:.1f}% ({summary['covered_lines']} of {summary['num_statements']} statements, "
          f"branch coverage included in the percentage)")
    if overall < OVERALL_FLOOR:
        failures.append(f"whole file: {overall:.1f}% is below the floor of {OVERALL_FLOOR}%")
    for message in failures:
        print("FAIL:", message)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
