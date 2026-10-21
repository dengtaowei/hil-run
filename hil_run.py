#!/usr/bin/env python3
"""Run serial plans against couart hubs and print a JSON verdict.

The process exit code is the result: 0 passes, 1 fails. Plans name logical
devices; the bench file binds those names to couart hub instances. A write
step takes the hub TX lock, writes one line, waits for the shell prompt, then
applies the step's comparators to that reply. A read step sends nothing: it
waits for UART bytes that arrived on that device since the previous step and
applies the same comparators. That covers an interrupt printed on another
core after this core has already returned to the prompt. A sleep_ms step
sends nothing: the host waits that many milliseconds, then judges UART text
that arrived during the wait. The wait uses the host clock.

Comparators (all must pass):
  pattern       regular expression, re.MULTILINE
  count         pattern must match this many times
  capture_set   group 1 values are exactly this set, each once
                ("0..15" or a list)
  range         group 1 is an integer inside {min, max} inclusive
  gt ge eq lt le
                group 1 compared with an integer, or with a name saved earlier
  save          keep group 1 as an integer under this name for a later step
  absent        regular expression must not match
  equals        the new UART text, prompts stripped, equals this string

A write string may contain ${name}; that is replaced from names saved in
this plan. save / ${name} / gt / ge / eq / lt / le are for serial steps.

Stdout stays a short JSON verdict. The judged UART window for each step is
written under tests/hil/reports/<time>/ with summary.txt, report.json, and
windows/. --verify reruns the same comparators on those windows and does not
open a serial port.

Firmware print lines used by plans should follow tests/hil/uart.md.
"""

import argparse
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

TAIL_LIMIT = 1500


class PlanError(Exception):
    pass


class StepFailure(Exception):
    def __init__(self, step_id, message, received, checked=False):
        super().__init__(message)
        self.step_id = step_id
        self.received = received
        self.checked = checked


def control_path(hub):
    root = os.environ.get("XDG_RUNTIME_DIR")
    if root:
        return os.path.join(root, "couart", hub, "control.sock")
    return "/tmp/couart-%d/%s/control.sock" % (os.getuid(), hub)


def ctl(hub, cmd, timeout_s):
    path = control_path(hub)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        sock.connect(path)
        sock.sendall((cmd + "\n").encode())
        chunks = []
        while True:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
    except OSError as exc:
        raise PlanError("%s: %s" % (hub, exc)) from exc
    finally:
        sock.close()
    return b"".join(chunks).decode("utf-8", "replace")


def tx_lock(hub, timeout_s):
    deadline = time.monotonic() + timeout_s
    while True:
        reply = ctl(hub, "TXLOCK", min(2.0, timeout_s))
        if reply.startswith("OK"):
            match = re.search(r"id=(\d+)", reply)
            if not match:
                raise PlanError("%s: TXLOCK reply has no id" % hub)
            return int(match.group(1))
        if time.monotonic() >= deadline:
            raise PlanError("%s: TX lock busy" % hub)
        time.sleep(0.03)


def tx_unlock(hub, lock_id):
    try:
        ctl(hub, "TXUNLOCK %d" % lock_id, 2.0)
    except PlanError:
        pass


def history(hub, want):
    reply = ctl(hub, "HISTORY %d" % want, 2.0)
    if not reply.startswith("OK"):
        raise PlanError("%s: HISTORY failed: %s" % (hub, reply.split("\n", 1)[0]))
    seq_match = re.search(r"seq=(\d+)", reply)
    seq = int(seq_match.group(1)) if seq_match else 0
    hex_part = reply.split("\n", 1)[1].strip() if "\n" in reply else ""
    if len(hex_part) % 2:
        raise PlanError("%s: HISTORY hex truncated" % hub)
    raw = bytes.fromhex(hex_part).decode("utf-8", "replace") if hex_part else ""
    return seq, raw


def send_command(hub, command, prompt, timeout_s):
    lock_id = tx_lock(hub, timeout_s)
    try:
        seq0, _ = history(hub, 256)
        payload = command.rstrip("\r\n").encode() + b"\r"
        reply = ctl(hub, "WRITE id=%d %s" % (lock_id, payload.hex()), 2.0)
        if not reply.startswith("OK"):
            raise PlanError("%s: write failed: %s" % (hub, reply.strip()))
        deadline = time.monotonic() + timeout_s
        prompt_re = re.compile(prompt)
        last = ""
        while time.monotonic() < deadline:
            seq1, raw = history(hub, 16384)
            new_n = max(0, seq1 - seq0)
            suffix = raw[-new_n:] if new_n else ""
            last = suffix
            if prompt_re.search(suffix):
                return normalize(suffix), seq1
            time.sleep(0.05)
        raise StepFailure(
            "",
            "timeout waiting for %s" % prompt,
            normalize(last),
            checked=False,
        )
    finally:
        tx_unlock(hub, lock_id)


class StuckDevices(object):
    """Cores whose shell did not return. Later steps must not transmit to them."""

    def __init__(self):
        self._devices = []
        self.lock = threading.Lock()

    def mark(self, device):
        with self.lock:
            if device not in self._devices:
                self._devices.append(device)
                print("stuck %s  needs reset" % device, file=sys.stderr)

    def has(self, device):
        with self.lock:
            return device in self._devices

    def names(self):
        with self.lock:
            return list(self._devices)


class Cursors(object):
    """Per-hub UART byte cursor. Read steps consume only bytes after it."""

    def __init__(self):
        self.seq = {}
        self.lock = threading.Lock()

    def get(self, hub):
        with self.lock:
            if hub not in self.seq:
                seq, _ = history(hub, 256)
                self.seq[hub] = seq
            return self.seq[hub]

    def set(self, hub, seq):
        with self.lock:
            self.seq[hub] = seq


def count_overshoot(text, step):
    """Fail when a counted pattern already has more hits than requested.

    More UART text can only add matches, so a count that is already too high
    will not become exact.
    """
    for item in step.get("expect", []):
        pattern = item.get("pattern")
        if pattern is None or "count" not in item:
            continue
        found = len(list(re.finditer(pattern, text, re.M)))
        want = int(item["count"])
        if found > want:
            claim = {k: item[k] for k in item if k in (
                "pattern", "absent", "equals", "count", "capture_set", "range")}
            reason = "pattern matched %d times, want %d %s" % (
                found, want, json.dumps(claim, ensure_ascii=False))
            return StepFailure(step_id(step, 0), reason, text, checked=True)
    return None


def read_new(hub, cursors, timeout_s, step, store):
    """Wait until this device's new UART text satisfies the step."""
    seq0 = cursors.get(hub)
    deadline = time.monotonic() + timeout_s
    last_exc = None
    seq1 = seq0
    while True:
        seq1, raw = history(hub, 16384)
        new_n = max(0, seq1 - seq0)
        if new_n > len(raw):
            new_n = len(raw)
        text = normalize(raw[-new_n:] if new_n else "")
        try:
            judge(step, text, store)
        except StepFailure as exc:
            last_exc = exc
            over = count_overshoot(text, step)
            if over:
                cursors.set(hub, seq1)
                raise over
        else:
            cursors.set(hub, seq1)
            return text
        if time.monotonic() >= deadline:
            cursors.set(hub, seq1)
            if last_exc:
                raise last_exc
            raise StepFailure(
                step_id(step, 0), "timeout waiting for unread uart text", text, checked=True)
        time.sleep(0.05)


def normalize(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_capture_set(value):
    if isinstance(value, str):
        match = re.fullmatch(r"(-?\d+)\.\.(-?\d+)", value.strip())
        if not match:
            raise PlanError("capture_set %r is not a list or N..M" % value)
        start, end = int(match.group(1)), int(match.group(2))
        step = 1 if end >= start else -1
        return [str(i) for i in range(start, end + step, step)]
    if isinstance(value, list):
        return [str(item) for item in value]
    raise PlanError("capture_set must be a list or N..M")


REL_OPS = ("gt", "ge", "eq", "lt", "le")
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def capture_int(match):
    raw = match.group(1) if match.groups() else match.group(0)
    try:
        return int(raw, 0)
    except ValueError:
        return None


def resolve_ref(value, store):
    if isinstance(value, bool) or isinstance(value, float):
        raise PlanError("relation value must be an integer or a saved name")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value not in store:
            raise PlanError("unknown saved value %r" % value)
        return int(store[value])
    raise PlanError("relation value must be an integer or a saved name")


def rel_holds(value, op, ref):
    if op == "gt":
        return value > ref
    if op == "ge":
        return value >= ref
    if op == "eq":
        return value == ref
    if op == "lt":
        return value < ref
    if op == "le":
        return value <= ref
    raise PlanError("unknown relation %s" % op)


def check_expect(text, expect, store=None):
    """Return None on success, or a short reason.

    store is the plan's saved integers. save writes group 1 after the other
    checks on this entry pass.
    """
    if store is None:
        store = {}
    if "absent" in expect:
        found = re.search(expect["absent"], text, re.M)
        if found:
            return "absent pattern matched: %s" % found.group(0)
        return None

    if "equals" in expect:
        body = re.sub(r"(?m)^.*boot>.*\n?", "", text).strip()
        if body != str(expect["equals"]):
            return "equals mismatch"
        return None

    pattern = expect.get("pattern")
    if pattern is None:
        raise PlanError("expect entry needs pattern, absent, or equals")
    matches = list(re.finditer(pattern, text, re.M))
    count = expect.get("count")
    if count is not None and len(matches) != int(count):
        return "pattern matched %d times, want %d" % (len(matches), int(count))
    if count is None and not matches:
        return "pattern did not match"

    if "capture_set" in expect:
        wanted = parse_capture_set(expect["capture_set"])
        got = [m.group(1) if m.groups() else m.group(0) for m in matches]
        if sorted(got) != sorted(wanted) or len(got) != len(set(got)):
            return "capture set %s, want %s" % (got, wanted)

    if "range" in expect:
        bounds = expect["range"]
        lo, hi = bounds["min"], bounds["max"]
        for match in matches:
            value = capture_int(match)
            if value is None:
                raw = match.group(1) if match.groups() else match.group(0)
                return "range capture %r is not an integer" % raw
            if value < lo or value > hi:
                return "value %s outside %s..%s" % (value, lo, hi)

    rels = [key for key in REL_OPS if key in expect]
    if len(rels) > 1:
        raise PlanError("expect entry has more than one of gt, ge, eq, lt, le")
    if rels:
        op = rels[0]
        ref = resolve_ref(expect[op], store)
        for match in matches:
            value = capture_int(match)
            if value is None:
                raw = match.group(1) if match.groups() else match.group(0)
                return "relation capture %r is not an integer" % raw
            if not rel_holds(value, op, ref):
                return "value %s not %s %s" % (value, op, ref)

    if "save" in expect:
        if not matches or not matches[0].groups():
            return "save needs a capture group"
        value = capture_int(matches[0])
        if value is None:
            return "save capture %r is not an integer" % matches[0].group(1)
        store[expect["save"]] = value
    return None


def expand_refs(text, store):
    def repl(match):
        key = match.group(1)
        if key not in store:
            raise PlanError("unknown value ${%s}" % key)
        return str(store[key])

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, text)


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise PlanError("%s: expected a mapping" % path)
    return data


def step_action(step):
    if isinstance(step.get("write"), str) and step["write"]:
        return step["write"]
    if "sleep_ms" in step:
        return "sleep %sms" % step["sleep_ms"]
    return "read"


def validate_step(step, devices, where):
    if not isinstance(step, dict):
        raise PlanError("%s: step is not a mapping" % where)
    if "parallel" in step:
        raise PlanError("%s: nested parallel" % where)
    device = step.get("device")
    if device not in devices:
        raise PlanError("%s: unknown device %r" % (where, device))
    has_write = isinstance(step.get("write"), str) and bool(step["write"])
    is_read = step.get("read") is True
    has_sleep = "sleep_ms" in step
    if sum((has_write, is_read, has_sleep)) != 1:
        raise PlanError("%s: step needs exactly one of write, read: true, sleep_ms" % where)
    if has_sleep:
        ms = step["sleep_ms"]
        if isinstance(ms, bool) or not isinstance(ms, int) or ms < 0:
            raise PlanError("%s: sleep_ms must be a non-negative integer" % where)
    if is_read and step.get("mutating"):
        raise PlanError("%s: a read step cannot be mutating" % where)
    expect = step.get("expect", [])
    if (is_read or has_sleep) and not expect:
        raise PlanError("%s: a read or sleep step needs expect" % where)
    if not isinstance(expect, list):
        raise PlanError("%s: expect must be a list" % where)
    for item in expect:
        if not isinstance(item, dict):
            raise PlanError("%s: expect entry must be a mapping" % where)
        keys = [k for k in ("pattern", "absent", "equals") if k in item]
        if len(keys) != 1:
            raise PlanError("%s: expect entry needs one of pattern, absent, equals" % where)
        if "range" in item and not isinstance(item["range"], dict):
            raise PlanError("%s: range must be {min, max}" % where)
        rels = [key for key in REL_OPS if key in item]
        if len(rels) > 1:
            raise PlanError("%s: use one of gt, ge, eq, lt, le" % where)
        if ("save" in item or rels) and "pattern" not in item:
            raise PlanError("%s: save and gt/ge/eq/lt/le need a pattern" % where)
        if "save" in item and not (isinstance(item["save"], str) and NAME_RE.fullmatch(item["save"])):
            raise PlanError("%s: save name must be an identifier" % where)
        for key in rels:
            value = item[key]
            if isinstance(value, bool) or not (
                isinstance(value, int) or (isinstance(value, str) and NAME_RE.fullmatch(value))
            ):
                raise PlanError("%s: %s must be an integer or a saved name" % (where, key))


def iter_steps(items, devices, where):
    if not isinstance(items, list):
        raise PlanError("%s: steps must be a list" % where)
    for index, step in enumerate(items):
        loc = "%s[%d]" % (where, index)
        if isinstance(step, dict) and "parallel" in step:
            group = step["parallel"]
            if not isinstance(group, list) or not group:
                raise PlanError("%s: parallel group is empty" % loc)
            seen = set()
            for child in group:
                if child.get("mutating"):
                    raise PlanError("%s: mutating step inside parallel" % loc)
                validate_step(child, devices, loc)
                if child["device"] in seen:
                    raise PlanError("%s: device %s appears twice in one parallel group" % (loc, child["device"]))
                seen.add(child["device"])
            yield step
            continue
        validate_step(step, devices, loc)
        yield step


def step_id(step, index):
    if step.get("id"):
        return str(step["id"])
    return "%s[%d] %s" % (step.get("device", "?"), index, step_action(step))


def judge(step, text, store=None):
    for item in step.get("expect", []):
        reason = check_expect(text, item, store)
        if reason:
            claim = claim_of(item)
            raise StepFailure(
                step_id(step, 0),
                reason + " " + json.dumps(claim, ensure_ascii=False),
                text,
                checked=True,
            )


CLAIM_KEYS = (
    "pattern", "absent", "equals", "count", "capture_set", "range",
    "save", "gt", "ge", "eq", "lt", "le",
)


def claim_of(item):
    return {key: item[key] for key in item if key in CLAIM_KEYS}


def compact_span(values):
    if not values:
        return ""
    try:
        nums = [int(value, 0) for value in values]
    except ValueError:
        shown = ",".join(str(value) for value in values[:8])
        if len(values) > 8:
            shown += ",..."
        return shown
    uniq = sorted(set(nums))
    if uniq == list(range(uniq[0], uniq[-1] + 1)) and len(uniq) == len(nums):
        if uniq[0] == uniq[-1]:
            return str(uniq[0])
        return "%d..%d" % (uniq[0], uniq[-1])
    shown = ",".join(str(num) for num in uniq[:12])
    if len(uniq) > 12:
        shown += ",..."
    return shown


def match_item(text, item):
    if "absent" in item:
        found = re.search(item["absent"], text or "", re.M)
        rec = {"absent": item["absent"], "hit": bool(found)}
        if found:
            rec["sample"] = found.group(0)[:200]
        return rec
    if "equals" in item:
        body = re.sub(r"(?m)^.*boot>.*\n?", "", text or "").strip()
        return {"equals": str(item["equals"]), "hit": body == str(item["equals"])}
    pattern = item.get("pattern")
    matches = list(re.finditer(pattern, text or "", re.M)) if pattern else []
    rec = {"pattern": pattern, "count": len(matches)}
    if matches and matches[0].groups():
        rec["captures"] = [match.group(1) for match in matches]
    if matches:
        rec["sample"] = matches[0].group(0)[:200]
    if "count" in item:
        rec["want"] = int(item["count"])
    if "capture_set" in item:
        rec["capture_set"] = item["capture_set"]
    if "range" in item:
        rec["range"] = item["range"]
    return rec


def describe_item(text, item):
    rec = match_item(text, item)
    if "absent" in rec:
        return 'absent "%s"' % rec["absent"]
    if "equals" in rec:
        return "equals"
    if "captures" in rec and (item.get("count") is not None or "capture_set" in item):
        return "%d 行 %s  %s" % (rec["count"], compact_span(rec["captures"]), rec.get("sample", ""))
    if rec.get("sample"):
        return rec["sample"]
    return "无匹配"


def describe_step(text, step):
    parts = [describe_item(text, item) for item in step.get("expect", [])]
    return " | ".join(part for part in parts if part)


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


def make_record(step, ok, text, error, checked, skipped=False):
    text = text or ""
    return {
        "id": step_id(step, 0),
        "device": step.get("device", ""),
        "action": step_action(step),
        "ok": ok,
        "checked": checked,
        "skipped": skipped,
        "expect": [claim_of(item) for item in step.get("expect", [])],
        "matches": [match_item(text, item) for item in step.get("expect", [])] if checked else [],
        "summary": describe_step(text, step) if checked else (error or ""),
        "text": text,
        "error": error or "",
        "cleanup": False,
    }


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
    if not record["ok"]:
        out["received_tail"] = tail(record["text"])
    return out


def verify_report(directory):
    path = os.path.join(directory, "report.json")
    with open(path, "r", encoding="utf-8") as fh:
        report = json.load(fh)
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
            rel = step.get("window")
            if not rel:
                errors.append("%s: checked step has no window" % label)
                continue
            wpath = os.path.join(directory, *rel.split("/"))
            with open(wpath, "r", encoding="utf-8") as fh:
                text = fh.read()
            if text_sha256(text) != step.get("window_sha256"):
                errors.append("%s: window sha256 mismatch" % label)
                continue
            reasons = []
            for item in step.get("expect", []):
                reason = check_expect(text, item, store)
                if reason:
                    reasons.append(reason)
            passed = not reasons
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


def host_sleep(hub, cursors, sleep_ms, step, store):
    """Wait on the host clock, then judge UART text that arrived during it.

    Unlike a read step, this does not return when the pattern first matches.
    The full sleep is the measurement window.
    """
    seq0 = cursors.get(hub)
    time.sleep(sleep_ms / 1000.0)
    seq1, raw = history(hub, 16384)
    new_n = max(0, seq1 - seq0)
    if new_n > len(raw):
        new_n = len(raw)
    text = normalize(raw[-new_n:] if new_n else "")
    cursors.set(hub, seq1)
    judge(step, text, store)
    return text


def run_step(step, bench, cursors, stuck, timeout_s, prompt, store):
    device = step["device"]
    if stuck.has(device):
        raise StepFailure(step_id(step, 0), "%s needs reset" % device, "")
    hub = bench["devices"][device]
    try:
        if step.get("read") is True:
            text = read_new(hub, cursors, timeout_s, step, store)
        elif "sleep_ms" in step:
            text = host_sleep(hub, cursors, int(step["sleep_ms"]), step, store)
        else:
            command = expand_refs(step["write"], store)
            try:
                text, seq = send_command(hub, command, prompt, timeout_s)
            except StepFailure:
                # The command was written and the prompt never came back.
                stuck.mark(device)
                raise
            cursors.set(hub, seq)
            judge(step, text, store)
    except StepFailure as exc:
        exc.step_id = step_id(step, 0)
        raise
    return text


def run_group(steps, bench, cursors, stuck, timeout_s, prompt, store, skip_stuck=False):
    """Run steps. A parallel mapping runs its children together.

    Records stay in plan order. The return is (failures, records).
    """
    failures = []
    records = []

    def one(step):
        if stuck.has(step["device"]):
            record = make_record(
                step, False, "", "%s needs reset" % step["device"], False, skipped=True)
            if skip_stuck:
                return record, None
            return record, StepFailure(record["id"], record["error"], "", checked=False)
        try:
            text = run_step(step, bench, cursors, stuck, timeout_s, prompt, store)
            return make_record(step, True, text, "", True), None
        except (StepFailure, PlanError) as exc:
            received = exc.received if isinstance(exc, StepFailure) else ""
            checked = bool(getattr(exc, "checked", False))
            sid = exc.step_id if isinstance(exc, StepFailure) and exc.step_id else step_id(step, 0)
            record = make_record(step, False, received, str(exc), checked)
            record["id"] = sid
            failure = exc if isinstance(exc, StepFailure) else StepFailure(sid, str(exc), received)
            failure.step_id = sid
            return record, failure

    def take(step, record, failure):
        records.append(record)
        print(format_step_line(record), file=sys.stderr)
        if failure is not None:
            failures.append(failure)

    for step in steps:
        if isinstance(step, dict) and "parallel" in step:
            children = step["parallel"]
            with ThreadPoolExecutor(max_workers=len(children)) as pool:
                futures = {pool.submit(one, child): child for child in children}
                got = {}
                for future in as_completed(futures):
                    got[id(futures[future])] = future.result()
            for child in children:
                record, failure = got[id(child)]
                take(child, record, failure)
            if failures:
                break
            continue
        record, failure = one(step)
        take(step, record, failure)
        if failure is not None:
            break
    return failures, records


def tail(text):
    text = text or ""
    if len(text) <= TAIL_LIMIT:
        return text
    return text[-TAIL_LIMIT:]


def run_plan(plan, bench, stuck):
    prompt = plan.get("prompt", bench.get("prompt", "boot>"))
    timeout_s = float(plan.get("timeout_s", bench.get("timeout_s", 8)))
    devices = bench["devices"]
    steps = list(iter_steps(plan.get("steps", []), devices, plan.get("name", "plan")))
    cleanup = list(iter_steps(plan.get("cleanup", []), devices, "cleanup"))
    print("plan %s" % plan.get("name"), file=sys.stderr)
    cursors = Cursors()
    store = {}
    failures, records = run_group(steps, bench, cursors, stuck, timeout_s, prompt, store)
    if cleanup:
        cleanup_failures, cleanup_records = run_group(
            cleanup, bench, cursors, stuck, timeout_s, prompt, store, skip_stuck=True)
    else:
        cleanup_failures, cleanup_records = [], []
    for record in cleanup_records:
        record["cleanup"] = True
    records.extend(cleanup_records)
    reset = stuck.names()
    if not failures and not cleanup_failures:
        report = {"name": plan.get("name"), "ok": True}
        if reset:
            report["reset_required"] = reset
        return report, records
    primary = failures[0] if failures else cleanup_failures[0]
    report = {
        "name": plan.get("name"),
        "ok": False,
        "failed_step": primary.step_id,
        "error": str(primary),
        "received_tail": tail(primary.received),
    }
    if failures and cleanup_failures:
        report["cleanup_error"] = "%s: %s" % (cleanup_failures[0].step_id, cleanup_failures[0])
    elif cleanup_failures:
        report["failed_step"] = "cleanup:" + primary.step_id
    if reset:
        report["reset_required"] = reset
    return report, records


def self_test():
    owned = "\n".join(
        "channel %d : used : 1 owner id : 0" % i for i in range(16)
    ) + "\nboot> "
    reason = check_expect(owned, {
        "pattern": r"^channel (\d+) : used : 1 owner id : 0$",
        "count": 16,
        "capture_set": "0..15",
    })
    assert reason is None, reason
    short = "\n".join("channel %d : used : 0" % i for i in range(15))
    reason = check_expect(short, {
        "pattern": r"^channel (\d+) : used : 0$",
        "count": 16,
        "capture_set": "0..15",
    })
    assert reason and "matched 15" in reason, reason
    dup = owned.replace("boot>", "channel 0 : used : 1 owner id : 0\nboot>")
    reason = check_expect(dup, {
        "pattern": r"^channel (\d+) : used : 1 owner id : 0$",
        "capture_set": "0..15",
    })
    assert reason and "capture set" in reason, reason
    assert check_expect("ipcc init failed\n", {"absent": "ipcc init failed"})
    assert check_expect("temp=25C\n", {
        "pattern": r"temp=(\d+)",
        "range": {"min": 20, "max": 30},
    }) is None
    assert check_expect("temp=99C\n", {
        "pattern": r"temp=(\d+)",
        "range": {"min": 20, "max": 30},
    })
    store = {}
    assert check_expect("timer 0 count = 10\n", {
        "pattern": r"^timer 0 count = (\d+)$",
        "save": "c0",
    }, store) is None
    assert store["c0"] == 10
    assert check_expect("timer 0 count = 25\n", {
        "pattern": r"^timer 0 count = (\d+)$",
        "gt": "c0",
    }, store) is None
    assert check_expect("timer 0 count = 10\n", {
        "pattern": r"^timer 0 count = (\d+)$",
        "gt": "c0",
    }, store)
    assert check_expect("timer 3 count = 0\n", {
        "pattern": r"^timer 3 count = (\d+)$",
        "eq": 0,
    }, store) is None
    assert expand_refs("chiptest_timer get_count ${c0}", store) == "chiptest_timer get_count 10"
    try:
        expand_refs("chiptest_timer get_count ${missing}", store)
    except PlanError:
        pass
    else:
        raise AssertionError("unknown ${name} was accepted")
    devices = {"ap": "board_ap"}
    try:
        validate_step({
            "device": "ap",
            "write": "chiptest_timer get_count 0",
            "expect": [{"pattern": r"(\d+)", "gt": "c0", "eq": 1}],
        }, devices, "two-rel")
    except PlanError:
        pass
    else:
        raise AssertionError("two relations were accepted")
    validate_step({"device": "ap", "read": True, "expect": [{"pattern": "x"}]}, devices, "read")
    validate_step({"device": "ap", "sleep_ms": 1050, "expect": [{"pattern": "x"}]}, devices, "sleep")
    try:
        validate_step({
            "device": "ap", "sleep_ms": 1, "write": "x", "expect": [{"pattern": "x"}],
        }, devices, "sleep-write")
    except PlanError:
        pass
    else:
        raise AssertionError("sleep_ms and write together was accepted")
    try:
        validate_step({"device": "ap", "sleep_ms": -1, "expect": [{"pattern": "x"}]}, devices, "sleep-neg")
    except PlanError:
        pass
    else:
        raise AssertionError("negative sleep_ms was accepted")
    try:
        validate_step({"device": "ap", "read": True, "write": "ipcc status", "expect": [{"pattern": "x"}]}, devices, "both")
    except PlanError:
        pass
    else:
        raise AssertionError("read and write together was accepted")
    try:
        validate_step({"device": "ap", "read": True}, devices, "empty-read")
    except PlanError:
        pass
    else:
        raise AssertionError("read without expect was accepted")
    released = "\n".join("channel %d released" % (i % 16) for i in range(17))
    too_many = {
        "id": "too-many",
        "expect": [{
            "pattern": r"^channel (\d+) released$",
            "count": 16,
            "capture_set": "0..15",
        }],
    }
    over = count_overshoot(released, too_many)
    assert over is not None and "matched 17 times, want 16" in str(over), over
    short = "\n".join("channel %d released" % i for i in range(15))
    assert count_overshoot(short, too_many) is None
    stuck = StuckDevices()
    stuck.mark("peri")
    assert stuck.has("peri") and stuck.names() == ["peri"]
    stuck.mark("peri")
    assert stuck.names() == ["peri"]
    assert not stuck.has("ap")
    self_test_report()
    self_test_saved()
    print("self-test ok", file=sys.stderr)


def self_test_saved():
    first = {
        "id": "c1",
        "device": "ap",
        "write": "chiptest_timer get_count 0",
        "expect": [{"pattern": r"^timer 0 count = (\d+)$", "save": "c0"}],
    }
    second = {
        "id": "c2",
        "device": "ap",
        "write": "chiptest_timer get_count 0",
        "expect": [{"pattern": r"^timer 0 count = (\d+)$", "gt": "c0"}],
    }
    directory = tempfile.mkdtemp(prefix="hil-saved-")
    a = make_record(first, True, "timer 0 count = 10\nboot> \n", "", True)
    b = make_record(second, True, "timer 0 count = 40\nboot> \n", "", True)
    write_windows(directory, "tick", [a, b])
    report = {
        "ok": True,
        "plans": [{
            "name": "tick",
            "ok": True,
            "steps": [record_json(a), record_json(b)],
        }],
    }
    with open(os.path.join(directory, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh)
    assert verify_report(directory) == 0
    b_bad = make_record(second, False, "timer 0 count = 10\nboot> \n", "value 10 not gt 10", True)
    write_windows(directory, "tick-bad", [a, b_bad])
    report["plans"].append({
        "name": "tick-bad",
        "ok": False,
        "steps": [record_json(a), record_json(b_bad)],
    })
    with open(os.path.join(directory, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh)
    assert verify_report(directory) == 0


def self_test_report():
    text = "\n".join("channel %d released" % i for i in range(16)) + "\n"
    step = {
        "id": "saw-release",
        "device": "ap",
        "read": True,
        "expect": [{
            "pattern": r"^channel (\d+) released$",
            "count": 16,
            "capture_set": "0..15",
        }],
    }
    directory = tempfile.mkdtemp(prefix="hil-report-")
    record = make_record(step, True, text, "", True)
    assert "16 行 0..15" in record["summary"], record["summary"]
    write_windows(directory, "demo", [record])
    report = {
        "ok": True,
        "plans": [{
            "name": "demo",
            "ok": True,
            "steps": [record_json(record)],
        }],
    }
    with open(os.path.join(directory, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh)
    assert verify_report(directory) == 0
    short = "\n".join("channel %d released" % i for i in range(15)) + "\n"
    failed = make_record(step, False, short, "pattern matched 15 times, want 16", True)
    write_windows(directory, "demo-fail", [failed])
    report["plans"].append({
        "name": "demo-fail",
        "ok": False,
        "steps": [record_json(failed)],
    })
    with open(os.path.join(directory, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh)
    assert verify_report(directory) == 0
    window = os.path.join(directory, *record["window"].split("/"))
    with open(window, "a", encoding="utf-8") as fh:
        fh.write("channel 0 released\n")
    assert verify_report(directory) == 1
    with open(window, "w", encoding="utf-8") as fh:
        fh.write(text)
    report["plans"][0]["steps"][0]["window_sha256"] = "0" * 64
    with open(os.path.join(directory, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh)
    assert verify_report(directory) == 1


def main(argv):
    parser = argparse.ArgumentParser(description="Run couart serial plans")
    parser.add_argument("plans", nargs="*", help="plan YAML files or a directory")
    parser.add_argument("--bench", help="bench YAML (default: tests/hil/bench.yaml)")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--report-dir", help="directory for summary.txt, report.json, and windows")
    parser.add_argument("--verify", help="recheck a saved report directory without a serial port")
    args = parser.parse_args(argv)
    if args.verify:
        return verify_report(args.verify)
    if args.self_test:
        self_test()
        if not args.plans:
            return 0

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bench_path = args.bench or os.path.join(root, "tests", "hil", "bench.yaml")
    bench = load_yaml(bench_path)
    if "devices" not in bench:
        raise PlanError("%s: missing devices" % bench_path)

    paths = []
    for item in args.plans:
        if os.path.isdir(item):
            paths.extend(
                os.path.join(item, name)
                for name in sorted(os.listdir(item))
                if name.endswith((".yaml", ".yml"))
            )
        else:
            paths.append(item)
    if not paths:
        parser.error("give at least one plan")

    directory = args.report_dir or fresh_report_dir(root)
    if args.report_dir:
        os.makedirs(directory, exist_ok=True)
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    reports = []
    detailed = []
    summary_lines = ["started %s" % started, "bench %s" % bench_path, ""]
    stuck = StuckDevices()
    for path in paths:
        plan = load_yaml(path)
        plan.setdefault("name", os.path.basename(path))
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
        "bench": {
            "path": bench_path,
            "sha256": file_sha256(bench_path),
            "devices": bench["devices"],
        },
        "plans": detailed,
    }
    with open(os.path.join(directory, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    with open(os.path.join(directory, "summary.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(summary_lines).rstrip() + "\n")
    print("report %s" % directory, file=sys.stderr)
    result = {"ok": ok, "report_dir": directory, "plans": reports}
    if stuck.names():
        result["reset_required"] = stuck.names()
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except PlanError as exc:
        json.dump({"ok": False, "error": str(exc)}, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        print(exc, file=sys.stderr)
        sys.exit(1)
