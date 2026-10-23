"""Run plan steps against couart hubs."""

import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import couart
from .compare import claim_text, check_expect
from .errors import PlanError, StepFailure
from .plan import expand_refs, step_id
from .report import make_record, format_step_line, tail

OVERSHOOT_CLAIM = ("pattern", "absent", "equals", "count", "capture_set", "range")


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
                seq, _, _ = couart.history_timed(hub, 256)
                self.seq[hub] = seq
            return self.seq[hub]

    def set(self, hub, seq):
        with self.lock:
            self.seq[hub] = seq


def judge(step, text, store=None, times=None):
    for item in step.get("expect", []):
        reason = check_expect(text, item, store, times)
        if reason:
            raise StepFailure(step_id(step), reason + " " + claim_text(item), text, checked=True)


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
            claim = {k: item[k] for k in item if k in OVERSHOOT_CLAIM}
            reason = "pattern matched %d times, want %d %s" % (
                found, want, json.dumps(claim, ensure_ascii=False))
            return StepFailure(step_id(step), reason, text, checked=True)
    return None


def send_command(hub, command, prompt, timeout_s):
    """Write one line under the TX lock and wait for the prompt.

    Returns text, times, seq. A missing prompt is an unchecked StepFailure.
    """
    lock_id = couart.tx_lock(hub, timeout_s)
    try:
        seq0, _, _ = couart.history_timed(hub, 256)
        couart.write_line(hub, lock_id, command)
        deadline = time.monotonic() + timeout_s
        prompt_re = re.compile(prompt)
        last = ""
        while time.monotonic() < deadline:
            text, times, seq1 = couart.new_text(hub, seq0)
            last = text
            if prompt_re.search(text):
                return text, times, seq1
            time.sleep(couart.POLL_S)
        raise StepFailure("", "timeout waiting for %s" % prompt, last, checked=False)
    finally:
        couart.tx_unlock(hub, lock_id)


def read_new(hub, cursors, timeout_s, step, store):
    """Wait until this device's new UART text satisfies the step."""
    seq0 = cursors.get(hub)
    deadline = time.monotonic() + timeout_s
    last_exc = None
    while True:
        text, tms, seq1 = couart.new_text(hub, seq0)
        try:
            judge(step, text, store, tms)
        except StepFailure as exc:
            last_exc = exc
            over = count_overshoot(text, step)
            if over:
                cursors.set(hub, seq1)
                raise over
        else:
            cursors.set(hub, seq1)
            return text, tms
        if time.monotonic() >= deadline:
            cursors.set(hub, seq1)
            if last_exc:
                raise last_exc
            raise StepFailure(step_id(step), "timeout waiting for unread uart text", text, checked=True)
        time.sleep(couart.POLL_S)


def host_sleep(hub, cursors, sleep_ms, step, store):
    """Wait on the host clock, then judge UART text that arrived during it.

    Unlike a read step, this does not return when the pattern first matches.
    The full sleep is the measurement window.
    """
    seq0 = cursors.get(hub)
    time.sleep(sleep_ms / 1000.0)
    text, tms, seq1 = couart.new_text(hub, seq0)
    cursors.set(hub, seq1)
    judge(step, text, store, tms)
    return text, tms


class PlanContext(object):
    """What every step in one plan run shares."""

    def __init__(self, devices, stuck, timeout_s, prompt):
        self.devices = devices
        self.stuck = stuck
        self.timeout_s = timeout_s
        self.prompt = prompt
        self.cursors = Cursors()
        self.store = {}


def run_step(step, ctx):
    device = step["device"]
    if ctx.stuck.has(device):
        raise StepFailure(step_id(step), "%s needs reset" % device, "")
    hub = ctx.devices[device]
    try:
        if step.get("read") is True:
            text, times = read_new(hub, ctx.cursors, ctx.timeout_s, step, ctx.store)
        elif "sleep_ms" in step:
            text, times = host_sleep(hub, ctx.cursors, int(step["sleep_ms"]), step, ctx.store)
        else:
            command = expand_refs(step["write"], ctx.store)
            try:
                text, times, seq = send_command(hub, command, ctx.prompt, ctx.timeout_s)
            except StepFailure:
                # The command was written and the prompt never came back.
                ctx.stuck.mark(device)
                raise
            ctx.cursors.set(hub, seq)
            judge(step, text, ctx.store, times)
    except StepFailure as exc:
        exc.step_id = step_id(step)
        raise
    return text, times


def run_one(step, ctx, skip_stuck):
    """Run one step. Returns (record, failure or None). Never raises a DUT verdict."""
    if ctx.stuck.has(step["device"]):
        record = make_record(step, False, "", "%s needs reset" % step["device"], False, skipped=True)
        if skip_stuck:
            return record, None
        return record, StepFailure(record["id"], record["error"], "", checked=False)
    try:
        text, times = run_step(step, ctx)
        return make_record(step, True, text, "", True, times=times, store=ctx.store), None
    except (StepFailure, PlanError) as exc:
        received = exc.received if isinstance(exc, StepFailure) else ""
        checked = bool(getattr(exc, "checked", False))
        sid = exc.step_id if isinstance(exc, StepFailure) and exc.step_id else step_id(step)
        record = make_record(step, False, received, str(exc), checked)
        record["id"] = sid
        failure = exc if isinstance(exc, StepFailure) else StepFailure(sid, str(exc), received)
        failure.step_id = sid
        return record, failure


def run_group(steps, ctx, skip_stuck=False):
    """Run steps. A parallel mapping runs its children together.

    Records stay in plan order. The return is (failures, records).
    """
    failures = []
    records = []

    def take(record, failure):
        records.append(record)
        print(format_step_line(record), file=sys.stderr)
        if failure is not None:
            failures.append(failure)

    for step in steps:
        if "parallel" in step:
            children = step["parallel"]
            with ThreadPoolExecutor(max_workers=len(children)) as pool:
                futures = {pool.submit(run_one, child, ctx, skip_stuck): index
                           for index, child in enumerate(children)}
                got = {}
                for future in as_completed(futures):
                    got[futures[future]] = future.result()
            for index in range(len(children)):
                take(*got[index])
            if failures:
                break
            continue
        record, failure = run_one(step, ctx, skip_stuck)
        take(record, failure)
        if failure is not None:
            break
    return failures, records


def run_plan(plan, bench, stuck):
    """plan must come from prepare_plan. Returns (short verdict, records)."""
    prompt = plan.get("prompt", bench.get("prompt", "boot>"))
    timeout_s = float(plan.get("timeout_s", bench.get("timeout_s", 8)))
    print("plan %s" % plan.get("name"), file=sys.stderr)
    ctx = PlanContext(bench["devices"], stuck, timeout_s, prompt)
    failures, records = run_group(plan["steps"], ctx)
    cleanup_failures, cleanup_records = [], []
    if plan["cleanup"]:
        cleanup_failures, cleanup_records = run_group(plan["cleanup"], ctx, skip_stuck=True)
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
