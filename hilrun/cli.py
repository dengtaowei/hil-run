"""Command line: run plans, --check them, --verify a report, --self-test."""

import argparse
import json
import os
import sys
import time

from . import __version__
from .errors import PlanError
from .plan import load_bench, load_yaml, plan_paths, prepare_plan
from .report import (
    file_sha256, format_step_line, fresh_report_dir, record_json, verify_report,
    write_report, write_windows,
)
from .runner import StuckDevices, run_plan

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def self_test():
    tests = os.path.join(HERE, "tests")
    try:
        import pytest
    except ImportError:
        print("self-test needs pytest: python3 -m pip install --user pytest", file=sys.stderr)
        return 1
    code = pytest.main(["-q", "-p", "no:cacheprovider", tests])
    if code == 0:
        print("self-test ok", file=sys.stderr)
    return int(code)


def check_plans(bench, paths):
    bad = 0
    for path in paths:
        try:
            prepare_plan(load_yaml(path), bench, os.path.basename(path))
            print("ok  %s" % path, file=sys.stderr)
        except PlanError as exc:
            bad += 1
            print("bad  %s  %s" % (path, exc), file=sys.stderr)
    print("check %s  %d plans" % ("ok" if not bad else "failed", len(paths)), file=sys.stderr)
    return 1 if bad else 0


def load_plans(bench, paths):
    """Check every plan before any serial step runs."""
    return [(path, prepare_plan(load_yaml(path), bench, os.path.basename(path))) for path in paths]


def run_plans(bench, bench_path, plans, directory):
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    reports = []
    detailed = []
    summary_lines = ["started %s" % started, "bench %s" % bench_path, ""]
    stuck = StuckDevices()
    for path, plan in plans:
        short, records = run_plan(plan, bench, stuck)
        write_windows(directory, plan["name"], records)
        reports.append(short)
        detailed.append({
            "name": plan["name"],
            "path": path,
            "sha256": file_sha256(path),
            "ok": short["ok"],
            "steps": [record_json(record) for record in records],
        })
        summary_lines.append("plan %s  %s" % (plan["name"], "ok" if short["ok"] else "fail"))
        summary_lines.extend(format_step_line(record) for record in records)
        summary_lines.append("")
        if not short["ok"]:
            break
    ok = all(item["ok"] for item in reports)
    bundle = {
        "ok": ok,
        "started": started,
        "hil_run": __version__,
        "bench": {
            "path": bench_path,
            "sha256": file_sha256(bench_path),
            "devices": bench["devices"],
        },
        "plans": detailed,
    }
    write_report(directory, bundle, summary_lines)
    print("report %s" % directory, file=sys.stderr)
    result = {"ok": ok, "report_dir": directory, "plans": reports}
    if stuck.names():
        result["reset_required"] = stuck.names()
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if ok else 1


def main(argv, root):
    """root is where tests/hil/bench.yaml and tests/hil/reports/ live by default."""
    parser = argparse.ArgumentParser(description="Run couart serial plans")
    parser.add_argument("plans", nargs="*", help="plan YAML files or a directory")
    parser.add_argument("--bench", help="bench YAML (default: tests/hil/bench.yaml)")
    parser.add_argument("--self-test", action="store_true", help="run hil-run's own tests")
    parser.add_argument("--check", action="store_true",
                        help="check plan schema against the bench and exit; no serial port")
    parser.add_argument("--report-dir", help="directory for summary.txt, report.json, and windows")
    parser.add_argument("--verify", help="recheck a saved report directory without a serial port")
    parser.add_argument("--version", action="version", version="hil-run " + __version__)
    args = parser.parse_args(argv)
    if args.verify:
        return verify_report(args.verify)
    if args.self_test:
        code = self_test()
        if code or not args.plans:
            return code

    bench_path = args.bench or os.path.join(root, "tests", "hil", "bench.yaml")
    bench = load_bench(bench_path)
    paths = plan_paths(args.plans)
    if not paths:
        parser.error("give at least one plan")
    if args.check:
        return check_plans(bench, paths)

    plans = load_plans(bench, paths)
    directory = args.report_dir or fresh_report_dir(root)
    if args.report_dir:
        os.makedirs(directory, exist_ok=True)
    return run_plans(bench, bench_path, plans, directory)


def entry(argv, root):
    try:
        return main(argv, root)
    except PlanError as exc:
        json.dump({"ok": False, "error": str(exc)}, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        print(exc, file=sys.stderr)
        return 1
