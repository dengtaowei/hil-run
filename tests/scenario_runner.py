"""Run tests/scenario against fake hubs and return masked artifacts.

Digits, hashes, and timestamp arrays vary per run; masking them leaves the
step order, verdicts, reasons, and report shape for comparison.
"""

import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fakehub import FakeHub, RtcBoard  # noqa: E402

SCENARIO = os.path.join(HERE, "scenario")
RUNS = (
    ("pass", True),
    ("hang", True),
    ("notime", False),
)


def mask(text):
    text = re.sub(r"[0-9a-f]{64}", "<sha>", text)
    text = re.sub(r"/tmp/[^\s\"]+", "<tmp>", text)
    return re.sub(r"\d+(\.\d+)?", "N", text)


def mask_report(path):
    with open(path, "r", encoding="utf-8") as fh:
        report = json.load(fh)
    for plan in report.get("plans", []):
        for step in plan.get("steps", []):
            if "times_ms" in step:
                step["times_ms"] = "<times>"
    return mask(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True))


def run_one(script, run, historyt, workdir):
    runtime = tempfile.mkdtemp(prefix="hil-xdg-", dir=workdir)
    hubs = [
        FakeHub(runtime, "fake_ap", RtcBoard(), historyt=historyt),
        FakeHub(runtime, "fake_hp", RtcBoard(), historyt=historyt),
    ]
    report_dir = os.path.join(workdir, "report-" + run)
    env = dict(os.environ, XDG_RUNTIME_DIR=runtime, PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = subprocess.run(
            [sys.executable, script,
             "--bench", os.path.join(SCENARIO, "bench.yaml"),
             "--report-dir", report_dir,
             os.path.join(SCENARIO, run)],
            env=env, capture_output=True, text=True, timeout=60,
        )
    finally:
        for hub in hubs:
            hub.close()
    verify = subprocess.run(
        [sys.executable, script, "--verify", report_dir],
        env=env, capture_output=True, text=True, timeout=60,
    )
    with open(os.path.join(report_dir, "summary.txt"), "r", encoding="utf-8") as fh:
        summary = fh.read()
    return {
        "exit": proc.returncode,
        "stdout": mask(proc.stdout),
        "stderr": mask(proc.stderr),
        "summary": mask("\n".join(summary.splitlines()[2:])),
        "report": mask_report(os.path.join(report_dir, "report.json")),
        "verify_exit": verify.returncode,
        "verify": mask(verify.stderr),
        "report_dir": report_dir,
    }


def run_all(script, workdir):
    return {run: run_one(script, run, historyt, workdir) for run, historyt in RUNS}


if __name__ == "__main__":
    out_dir = sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)
    results = run_all(os.path.abspath(sys.argv[1]), tempfile.mkdtemp(prefix="hil-golden-"))
    for run, result in results.items():
        for key in ("exit", "stdout", "stderr", "summary", "report", "verify_exit", "verify"):
            with open(os.path.join(out_dir, "%s.%s" % (run, key)), "w", encoding="utf-8") as fh:
                fh.write(str(result[key]) + "\n")
        print(run, "exit", result["exit"], "verify", result["verify_exit"], result["report_dir"])
