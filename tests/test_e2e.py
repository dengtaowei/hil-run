"""Run hil_run.py end to end against fake couart hubs."""

import json
import os
import subprocess
import sys

import pytest

import scenario_runner

HIL_RUN = os.path.join(os.path.dirname(scenario_runner.HERE), "hil_run.py")


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    return scenario_runner.run_all(HIL_RUN, str(tmp_path_factory.mktemp("e2e")))


def load(result):
    with open(os.path.join(result["report_dir"], "report.json"), encoding="utf-8") as fh:
        return json.load(fh)


def steps(report, plan):
    return {s["id"]: s for p in report["plans"] if p["name"] == plan for s in p["steps"]}


def test_pass_run(results):
    run = results["pass"]
    assert run["exit"] == 1
    report = load(run)
    assert [(p["name"], p["ok"]) for p in report["plans"]] == [
        ("tick", True), ("stop", True), ("alarm", True), ("parallel", True), ("fail", False)]
    tick = steps(report, "tick")
    rate = tick["tick-2"]["matches"][0]["rate_hz"]
    assert 32768 * 0.95 <= rate <= 32768 * 1.05
    assert tick["tick-2"]["expect"][0]["rate_hz"] == {"tick": "n1", "time": "t1", "hz": 32768, "tol_pct": 5}
    alarm = steps(report, "alarm")
    assert 800 <= alarm["irq"]["matches"][0]["elapsed_ms"] <= 1200
    assert alarm["irq"]["expect"][1]["range"]["min"] == 32768 * 0.95
    fail = steps(report, "fail")
    assert "never" not in fail
    assert fail["after"]["cleanup"] is True
    assert run["verify_exit"] == 0


def test_stuck_device_skips_its_cleanup(results):
    run = results["hang"]
    report = load(run)
    hang = steps(report, "hang")
    assert hang["hang"]["ok"] is False and "timeout waiting for boot>" in hang["hang"]["error"]
    assert hang["ap-cleanup"].get("skipped") is True
    assert hang["hp-cleanup"]["ok"] is True
    assert '"reset_required"' in run["stdout"]


def test_hub_without_historyt(results):
    run = results["notime"]
    report = load(run)
    notime = steps(report, "notime")
    assert notime["plain"]["ok"] is True
    assert "times_ms" not in notime["plain"]
    assert "no byte timestamp" in notime["timed"]["error"]


def test_check_mode(tmp_path):
    bench = os.path.join(scenario_runner.SCENARIO, "bench.yaml")
    ok = subprocess.run([sys.executable, HIL_RUN, "--check", "--bench", bench,
                         os.path.join(scenario_runner.SCENARIO, "pass")],
                        capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: bad\nsteps:\n  - device: ap\n    write: x\n    expect:\n"
                   "      - pattern: x\n        rate_hzz: {}\n")
    out = subprocess.run([sys.executable, HIL_RUN, "--check", "--bench", bench, str(bad)],
                         capture_output=True, text=True)
    assert out.returncode == 1
    assert "unknown expect key rate_hzz" in out.stderr


def test_bad_plan_stops_before_any_serial_step(tmp_path):
    bench = os.path.join(scenario_runner.SCENARIO, "bench.yaml")
    bad = tmp_path / "02_bad.yaml"
    bad.write_text("name: bad\nsteps:\n  - device: nope\n    write: x\n")
    good = tmp_path / "01_good.yaml"
    good.write_text("name: good\nsteps:\n  - device: ap\n    write: version\n")
    env = dict(os.environ, XDG_RUNTIME_DIR=str(tmp_path / "no-hubs"))
    out = subprocess.run([sys.executable, HIL_RUN, "--bench", bench,
                          "--report-dir", str(tmp_path / "r"), str(tmp_path)],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 1
    assert "unknown device 'nope'" in out.stdout
    assert not (tmp_path / "r").exists()
