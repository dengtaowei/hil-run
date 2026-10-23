import json
import os

from hilrun.report import (
    REPORT_FORMAT, make_record, record_json, verify_report, write_report, write_windows,
)


def save_report(directory, plans, fmt=None):
    report = {"ok": all(plan["ok"] for plan in plans), "plans": plans}
    if fmt is not None:
        report["format"] = fmt
    with open(os.path.join(directory, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh)
    return report


def plan_of(name, records, ok=True):
    return {"name": name, "ok": ok, "steps": [record_json(record) for record in records]}


FIRST = {"id": "c1", "device": "ap", "write": "chiptest_timer get_count 0",
         "expect": [{"pattern": r"^timer 0 count = (\d+)$", "save": "c0"}]}
SECOND = {"id": "c2", "device": "ap", "write": "chiptest_timer get_count 0",
          "expect": [{"pattern": r"^timer 0 count = (\d+)$", "gt": "c0"}]}


def test_verify_replays_saved_values(tmp_path):
    a = make_record(FIRST, True, "timer 0 count = 10\nboot> \n", "", True)
    b = make_record(SECOND, True, "timer 0 count = 40\nboot> \n", "", True)
    write_windows(str(tmp_path), "tick", [a, b])
    b_bad = make_record(SECOND, False, "timer 0 count = 10\nboot> \n", "value 10 not gt 10", True)
    write_windows(str(tmp_path), "tick-bad", [a, b_bad])
    save_report(str(tmp_path), [plan_of("tick", [a, b]), plan_of("tick-bad", [a, b_bad], ok=False)])
    assert verify_report(str(tmp_path)) == 0


RELEASE = {"id": "saw-release", "device": "ap", "read": True,
           "expect": [{"pattern": r"^channel (\d+) released$", "count": 16, "capture_set": "0..15"}]}
TEXT = "\n".join("channel %d released" % i for i in range(16)) + "\n"


def release_report(directory):
    record = make_record(RELEASE, True, TEXT, "", True)
    assert "16 行 0..15" in record["summary"]
    write_windows(directory, "demo", [record])
    short = "\n".join("channel %d released" % i for i in range(15)) + "\n"
    failed = make_record(RELEASE, False, short, "pattern matched 15 times, want 16", True)
    write_windows(directory, "demo-fail", [failed])
    report = save_report(directory, [plan_of("demo", [record]), plan_of("demo-fail", [failed], ok=False)])
    return record, report


def test_verify_detects_edited_window(tmp_path):
    record, _ = release_report(str(tmp_path))
    assert verify_report(str(tmp_path)) == 0
    window = os.path.join(str(tmp_path), *record["window"].split("/"))
    with open(window, "a", encoding="utf-8") as fh:
        fh.write("channel 0 released\n")
    assert verify_report(str(tmp_path)) == 1


def test_verify_detects_edited_hash(tmp_path):
    _, report = release_report(str(tmp_path))
    report["plans"][0]["steps"][0]["window_sha256"] = "0" * 64
    with open(os.path.join(str(tmp_path), "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh)
    assert verify_report(str(tmp_path)) == 1


def test_verify_missing_window_is_reported(tmp_path):
    record, _ = release_report(str(tmp_path))
    os.unlink(os.path.join(str(tmp_path), *record["window"].split("/")))
    assert verify_report(str(tmp_path)) == 1


def test_verify_replays_timestamps(tmp_path):
    step = {"id": "t", "device": "ap", "read": True, "expect": [
        {"pattern": r"^a$", "save_ms": "t0"},
        {"pattern": r"^b$", "elapsed_ms": {"since": "t0", "ms": 1000, "tol_pct": 5}}]}
    text = "a\nb\n"
    store = {}
    record = make_record(step, True, text, "", True, times=[0, 0, 1000, 1000], store=store)
    assert record["times_ms"] == [0, 0, 1000, 1000]
    write_windows(str(tmp_path), "t", [record])
    save_report(str(tmp_path), [plan_of("t", [record])])
    assert verify_report(str(tmp_path)) == 0


def test_report_format(tmp_path):
    write_report(str(tmp_path), {"ok": True, "plans": []}, ["x"])
    with open(os.path.join(str(tmp_path), "report.json"), encoding="utf-8") as fh:
        assert json.load(fh)["format"] == REPORT_FORMAT
    assert verify_report(str(tmp_path)) == 0
    save_report(str(tmp_path), [], fmt=REPORT_FORMAT + 1)
    assert verify_report(str(tmp_path)) == 1


def test_report_without_format_is_format_1(tmp_path):
    save_report(str(tmp_path), [])
    assert verify_report(str(tmp_path)) == 0
