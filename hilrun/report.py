"""Step records, report files, and --verify replay."""

import hashlib
import json
import os
import re
import sys
import time

from .compare import check_expect, claim_of, describe_expect, match_item
from .plan import step_action, step_id

TAIL_LIMIT = 1500
# Bump when report.json changes in a way --verify must know about.
# Reports without "format" are format 1.
REPORT_FORMAT = 1


def tail(text):
    text = text or ""
    if len(text) <= TAIL_LIMIT:
        return text
    return text[-TAIL_LIMIT:]


def safe_name(value):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._")
    return cleaned or "step"


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        digest.update(fh.read())
    return digest.hexdigest()


def text_sha256(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def make_record(step, ok, text, error, checked, skipped=False, times=None, store=None):
    text = text or ""
    expect = step.get("expect", [])
    record = {
        "id": step_id(step),
        "device": step.get("device", ""),
        "action": step_action(step),
        "ok": ok,
        "checked": checked,
        "skipped": skipped,
        "expect": [claim_of(item) for item in expect],
        "matches": [match_item(text, item, times, store) for item in expect] if checked else [],
        "summary": describe_expect(text, expect, times, store) if checked else (error or ""),
        "text": text,
        "error": error or "",
        "cleanup": False,
    }
    if times is not None and len(times) == len(text):
        record["times_ms"] = list(times)
    return record


def format_step_line(record):
    if record["skipped"]:
        status = "skip"
    elif record["ok"]:
        status = "ok"
    else:
        status = "fail"
    line = "%s  %s  %s" % (status, record["id"], record["action"])
    extra = record.get("summary") or ""
    if record.get("error") and record["error"] not in extra:
        extra = (extra + "  " + record["error"]).strip()
    if extra:
        line += "  " + extra
    return line


def write_windows(directory, plan_name, records):
    used = {}
    for record in records:
        if not record["text"] and not record["checked"]:
            record["window"] = ""
            record["window_sha256"] = ""
            continue
        base = safe_name(record["id"])
        used[base] = used.get(base, 0) + 1
        name = base if used[base] == 1 else "%s-%d" % (base, used[base])
        rel = "windows/%s/%s.txt" % (safe_name(plan_name), name)
        path = os.path.join(directory, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(record["text"])
        record["window"] = rel
        record["window_sha256"] = text_sha256(record["text"])


def record_json(record):
    out = {
        "id": record["id"],
        "device": record["device"],
        "action": record["action"],
        "ok": record["ok"],
        "checked": record["checked"],
        "expect": record["expect"],
        "matches": record["matches"],
        "window_sha256": record.get("window_sha256") or "",
    }
    if record.get("window"):
        out["window"] = record["window"]
    if record.get("cleanup"):
        out["cleanup"] = True
    if record["skipped"]:
        out["skipped"] = True
    if record.get("error"):
        out["error"] = record["error"]
    if record.get("times_ms") is not None:
        out["times_ms"] = record["times_ms"]
    if not record["ok"]:
        out["received_tail"] = tail(record["text"])
    return out


def fresh_report_dir(root):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(root, "tests", "hil", "reports", stamp)
    path = base
    number = 2
    while os.path.exists(path):
        path = "%s-%d" % (base, number)
        number += 1
    os.makedirs(path)
    return path


def write_report(directory, bundle, summary_lines):
    bundle = dict(bundle, format=REPORT_FORMAT)
    with open(os.path.join(directory, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    with open(os.path.join(directory, "summary.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(summary_lines).rstrip() + "\n")


def recheck_step(directory, step, store):
    """Return (passed, reasons) or raise ValueError with a report defect."""
    rel = step.get("window")
    if not rel:
        raise ValueError("checked step has no window")
    try:
        with open(os.path.join(directory, *rel.split("/")), "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ValueError("window unreadable: %s" % exc) from exc
    if text_sha256(text) != step.get("window_sha256"):
        raise ValueError("window sha256 mismatch")
    reasons = []
    for item in step.get("expect", []):
        reason = check_expect(text, item, store, step.get("times_ms"))
        if reason:
            reasons.append(reason)
    return not reasons, reasons


def verify_report(directory):
    with open(os.path.join(directory, "report.json"), "r", encoding="utf-8") as fh:
        report = json.load(fh)
    fmt = report.get("format", 1)
    if not isinstance(fmt, int) or fmt > REPORT_FORMAT:
        print("report format %r is newer than this hil-run (%d)" % (fmt, REPORT_FORMAT), file=sys.stderr)
        print("verify failed  1", file=sys.stderr)
        return 1
    errors = []
    checked_n = 0
    for plan in report.get("plans", []):
        store = {}
        for step in plan.get("steps", []):
            label = "%s/%s" % (plan.get("name"), step.get("id"))
            if not step.get("checked"):
                if step.get("ok"):
                    errors.append("%s: unchecked step recorded as passed" % label)
                continue
            try:
                passed, reasons = recheck_step(directory, step, store)
            except ValueError as exc:
                errors.append("%s: %s" % (label, exc))
                continue
            checked_n += 1
            if passed != bool(step.get("ok")):
                errors.append("%s: recheck ok=%s record ok=%s %s" % (
                    label, passed, bool(step.get("ok")), "; ".join(reasons)))
    if errors:
        for item in errors:
            print(item, file=sys.stderr)
        print("verify failed  %d" % len(errors), file=sys.stderr)
        return 1
    print("verify ok  %d steps" % checked_n, file=sys.stderr)
    return 0
