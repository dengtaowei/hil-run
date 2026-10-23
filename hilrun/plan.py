"""Plan and bench loading, schema checks, and bench clock expansion."""

import copy
import os
import re

import yaml

from .compare import is_number, tol_band, validate_expect
from .errors import PlanError

PLAN_KEYS = ("name", "steps", "cleanup", "timeout_s", "prompt")
STEP_KEYS = ("id", "device", "write", "read", "sleep_ms", "expect", "mutating")
REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise PlanError("%s: expected a mapping" % path)
    return data


def load_bench(path):
    bench = load_yaml(path)
    if "devices" not in bench:
        raise PlanError("%s: missing devices" % path)
    clocks = bench.get("clocks")
    if clocks is not None and not isinstance(clocks, dict):
        raise PlanError("%s: clocks must be a mapping" % path)
    return bench


def plan_paths(items):
    paths = []
    for item in items:
        if os.path.isdir(item):
            paths.extend(
                os.path.join(item, name)
                for name in sorted(os.listdir(item))
                if name.endswith((".yaml", ".yml"))
            )
        else:
            paths.append(item)
    return paths


def step_action(step):
    if isinstance(step.get("write"), str) and step["write"]:
        return step["write"]
    if "sleep_ms" in step:
        return "sleep %sms" % step["sleep_ms"]
    return "read"


def step_id(step, index=0):
    if step.get("id"):
        return str(step["id"])
    return "%s[%d] %s" % (step.get("device", "?"), index, step_action(step))


def expand_refs(text, store):
    def repl(match):
        key = match.group(1)
        if key not in store:
            raise PlanError("unknown value ${%s}" % key)
        return str(store[key])

    return REF_RE.sub(repl, text)


def validate_step(step, devices, where):
    if not isinstance(step, dict):
        raise PlanError("%s: step is not a mapping" % where)
    if "parallel" in step:
        raise PlanError("%s: nested parallel" % where)
    unknown = sorted(key for key in step if key not in STEP_KEYS)
    if unknown:
        raise PlanError("%s: unknown step key %s" % (where, ", ".join(unknown)))
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
        validate_expect(item, where)


def iter_steps(items, devices, where):
    if not isinstance(items, list):
        raise PlanError("%s: steps must be a list" % where)
    for index, step in enumerate(items):
        loc = "%s[%d]" % (where, index)
        if isinstance(step, dict) and "parallel" in step:
            if set(step) != {"parallel"}:
                raise PlanError("%s: a parallel group has only the parallel key" % loc)
            group = step["parallel"]
            if not isinstance(group, list) or not group:
                raise PlanError("%s: parallel group is empty" % loc)
            seen = set()
            for child in group:
                if isinstance(child, dict) and child.get("mutating"):
                    raise PlanError("%s: mutating step inside parallel" % loc)
                validate_step(child, devices, loc)
                if child["device"] in seen:
                    raise PlanError("%s: device %s appears twice in one parallel group" % (loc, child["device"]))
                seen.add(child["device"])
            yield step
            continue
        validate_step(step, devices, loc)
        yield step


def fill_clock(spec, clocks, where):
    name = spec.get("clock")
    table = clocks.get(name) if isinstance(clocks, dict) else None
    if not isinstance(name, str) or not isinstance(table, dict):
        raise PlanError("%s: unknown clock %r" % (where, name))
    if "hz" not in spec:
        if not is_number(table.get("hz")):
            raise PlanError("%s: clock %s needs hz" % (where, name))
        spec["hz"] = table["hz"]
    if "tol_pct" not in spec:
        if not is_number(table.get("tol_pct")) or table["tol_pct"] < 0:
            raise PlanError("%s: clock %s needs tol_pct" % (where, name))
        spec["tol_pct"] = table["tol_pct"]
    del spec["clock"]


def expand_clock_specs(items, clocks, where):
    """Resolve bench clocks in place.

    rate_hz {clock} gains that clock's hz and tol_pct. range {clock} or
    range {hz, tol_pct} becomes {min, max}. Callers that keep the plan must
    pass a copy; prepare_plan does.
    """
    if not isinstance(items, list):
        return
    for index, step in enumerate(items):
        if not isinstance(step, dict):
            continue
        loc = "%s[%d]" % (where, index)
        group = step["parallel"] if "parallel" in step else [step]
        if not isinstance(group, list):
            continue
        for child in group:
            if not isinstance(child, dict):
                continue
            for item in child.get("expect", []) or []:
                if not isinstance(item, dict):
                    continue
                spec = item.get("rate_hz")
                if isinstance(spec, dict) and "clock" in spec:
                    fill_clock(spec, clocks, loc)
                spec = item.get("range")
                if isinstance(spec, dict) and ("clock" in spec or "hz" in spec):
                    if "clock" in spec:
                        fill_clock(spec, clocks, loc)
                    if not is_number(spec.get("hz")) or not is_number(spec.get("tol_pct")):
                        raise PlanError("%s: range needs hz and tol_pct" % loc)
                    lo, hi = tol_band(spec["hz"], spec["tol_pct"])
                    spec["min"] = lo
                    spec["max"] = hi
                    del spec["hz"]
                    del spec["tol_pct"]


def prepare_plan(plan, bench, default_name="plan"):
    """Return a checked copy of plan with bench clocks resolved.

    The report records this copy's claims, so --verify needs no bench.
    """
    unknown = sorted(key for key in plan if key not in PLAN_KEYS)
    if unknown:
        raise PlanError("%s: unknown plan key %s" % (plan.get("name", default_name), ", ".join(unknown)))
    ready = copy.deepcopy(plan)
    ready.setdefault("name", default_name)
    clocks = bench.get("clocks") or {}
    devices = bench["devices"]
    expand_clock_specs(ready.get("steps", []), clocks, ready["name"])
    expand_clock_specs(ready.get("cleanup", []), clocks, "cleanup")
    ready["steps"] = list(iter_steps(ready.get("steps", []), devices, ready["name"]))
    ready["cleanup"] = list(iter_steps(ready.get("cleanup", []), devices, "cleanup"))
    return ready
