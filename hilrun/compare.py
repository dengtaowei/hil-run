"""Expect-entry comparators.

An expect entry has one selector (pattern, absent, equals). A pattern entry
may add comparators. Each comparator lists its YAML keys and owns its schema
check, verdict, report fields, and summary text, so adding one is one class
plus one line in COMPARATORS.

Order in COMPARATORS is the order of evaluation. Store writes (save,
save_ms) run only after every check on the entry passed.
"""

import json
import re

from .errors import PlanError

REL_OPS = ("gt", "ge", "eq", "lt", "le")
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SELECTORS = ("pattern", "absent", "equals")
PROMPT_LINE_RE = re.compile(r"(?m)^.*boot>.*\n?")


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_name(value):
    return isinstance(value, str) and bool(NAME_RE.fullmatch(value))


def capture_raw(match):
    return match.group(1) if match.groups() else match.group(0)


def capture_int(match):
    try:
        return int(capture_raw(match), 0)
    except ValueError:
        return None


def match_ms(match, times):
    """Host ms of the last byte of match. None if there are no stamps."""
    if not match or not times:
        return None
    idx = match.end() - 1
    if idx < 0 or idx >= len(times):
        return None
    return int(times[idx])


def tol_band(center, tol_pct):
    span = float(center) * float(tol_pct) / 100.0
    return float(center) - span, float(center) + span


def numeric_bounds(spec, center_key):
    """Absolute {min, max}, or {center_key, tol_pct} as center ± tol_pct %."""
    if "min" in spec:
        return spec["min"], spec["max"]
    return tol_band(spec[center_key], spec["tol_pct"])


def bounds_shape_ok(spec, center_key):
    has_band = "min" in spec or "max" in spec
    has_tol = center_key in spec or "tol_pct" in spec
    if has_band and has_tol:
        return False
    if has_band:
        return is_number(spec.get("min")) and is_number(spec.get("max"))
    if not is_number(spec.get(center_key)) or not is_number(spec.get("tol_pct")):
        return False
    return spec["tol_pct"] >= 0


def saved(store, name, what="value"):
    if name not in store:
        raise PlanError("unknown saved %s %r" % (what, name))
    return int(store[name])


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


def resolve_ref(value, store):
    if isinstance(value, bool) or isinstance(value, float):
        raise PlanError("relation value must be an integer or a saved name")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return saved(store, value)
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


class Context(object):
    """One pattern entry applied to one window."""

    def __init__(self, text, item, store, times):
        self.text = text or ""
        self.item = item
        self.store = store
        self.times = times
        self.matches = list(re.finditer(item["pattern"], self.text, re.M))
        self.first = self.matches[0] if self.matches else None
        self.now = match_ms(self.first, times)


class Comparator(object):
    keys = ()
    timed = False

    def owns(self, item):
        return any(key in item for key in self.keys)

    def validate(self, item, where):
        pass

    def check(self, ctx):
        return None

    def commit(self, ctx):
        pass

    def report(self, ctx, rec):
        pass

    def describe(self, rec):
        return ""


class Count(Comparator):
    keys = ("count",)

    def check(self, ctx):
        want = int(ctx.item["count"])
        if len(ctx.matches) != want:
            return "pattern matched %d times, want %d" % (len(ctx.matches), want)
        return None

    def report(self, ctx, rec):
        rec["want"] = int(ctx.item["count"])


class CaptureSet(Comparator):
    keys = ("capture_set",)

    def check(self, ctx):
        wanted = parse_capture_set(ctx.item["capture_set"])
        got = [capture_raw(m) for m in ctx.matches]
        if sorted(got) != sorted(wanted) or len(got) != len(set(got)):
            return "capture set %s, want %s" % (got, wanted)
        return None

    def report(self, ctx, rec):
        rec["capture_set"] = ctx.item["capture_set"]


class Range(Comparator):
    keys = ("range",)

    def validate(self, item, where):
        spec = item["range"]
        if not isinstance(spec, dict):
            raise PlanError("%s: range must be {min, max}" % where)

    def check(self, ctx):
        lo, hi = ctx.item["range"]["min"], ctx.item["range"]["max"]
        for match in ctx.matches:
            value = capture_int(match)
            if value is None:
                return "range capture %r is not an integer" % capture_raw(match)
            if value < lo or value > hi:
                return "value %s outside %s..%s" % (value, lo, hi)
        return None

    def report(self, ctx, rec):
        rec["range"] = ctx.item["range"]


class Relation(Comparator):
    keys = REL_OPS

    def validate(self, item, where):
        rels = [key for key in REL_OPS if key in item]
        if len(rels) > 1:
            raise PlanError("%s: use one of gt, ge, eq, lt, le" % where)
        value = item[rels[0]]
        if isinstance(value, bool) or not (isinstance(value, int) or is_name(value)):
            raise PlanError("%s: %s must be an integer or a saved name" % (where, rels[0]))

    def check(self, ctx):
        rels = [key for key in REL_OPS if key in ctx.item]
        if len(rels) > 1:
            raise PlanError("expect entry has more than one of gt, ge, eq, lt, le")
        op = rels[0]
        ref = resolve_ref(ctx.item[op], ctx.store)
        for match in ctx.matches:
            value = capture_int(match)
            if value is None:
                return "relation capture %r is not an integer" % capture_raw(match)
            if not rel_holds(value, op, ref):
                return "value %s not %s %s" % (value, op, ref)
        return None


class ElapsedMs(Comparator):
    """This match's arrival minus a save_ms time."""

    keys = ("elapsed_ms",)
    timed = True

    def validate(self, item, where):
        spec = item["elapsed_ms"]
        if not isinstance(spec, dict) or not is_name(spec.get("since")):
            raise PlanError("%s: elapsed_ms needs a since name" % where)
        if not bounds_shape_ok(spec, "ms"):
            raise PlanError("%s: elapsed_ms needs {min, max} or {ms, tol_pct}" % where)

    @staticmethod
    def value(ctx):
        return ctx.now - saved(ctx.store, ctx.item["elapsed_ms"]["since"], "time")

    def check(self, ctx):
        elapsed = self.value(ctx)
        lo, hi = numeric_bounds(ctx.item["elapsed_ms"], "ms")
        if elapsed < lo or elapsed > hi:
            return "elapsed %d ms outside %s..%s" % (elapsed, lo, hi)
        return None

    def report(self, ctx, rec):
        if ctx.item["elapsed_ms"]["since"] in ctx.store:
            rec["elapsed_ms"] = self.value(ctx)

    def describe(self, rec):
        return "  %d ms" % rec["elapsed_ms"] if "elapsed_ms" in rec else ""


class RateHz(Comparator):
    """(this capture - tick) * 1000 / (this arrival - time)."""

    keys = ("rate_hz",)
    timed = True

    def validate(self, item, where):
        spec = item["rate_hz"]
        if not isinstance(spec, dict):
            raise PlanError("%s: rate_hz needs tick and time" % where)
        tick = spec.get("tick")
        tick_ok = (isinstance(tick, int) and not isinstance(tick, bool)) or is_name(tick)
        if not tick_ok or not is_name(spec.get("time")):
            raise PlanError("%s: rate_hz needs tick and time" % where)
        if not bounds_shape_ok(spec, "hz"):
            raise PlanError("%s: rate_hz needs {min, max} or {hz, tol_pct}" % where)

    @staticmethod
    def value(ctx):
        """Return (rate, reason). rate is None when reason is set."""
        spec = ctx.item["rate_hz"]
        tick0 = spec["tick"]
        if isinstance(tick0, str):
            tick0 = saved(ctx.store, tick0)
        t0 = saved(ctx.store, spec["time"], "time")
        tick1 = capture_int(ctx.first)
        if tick1 is None:
            return None, "rate capture %r is not an integer" % capture_raw(ctx.first)
        dt = ctx.now - t0
        if dt <= 0:
            return None, "rate interval %d ms" % dt
        return (tick1 - tick0) * 1000.0 / dt, None

    def check(self, ctx):
        rate, reason = self.value(ctx)
        if reason:
            return reason
        lo, hi = numeric_bounds(ctx.item["rate_hz"], "hz")
        if rate < lo or rate > hi:
            return "rate %.1f Hz outside %s..%s" % (rate, lo, hi)
        return None

    def report(self, ctx, rec):
        try:
            rate, _ = self.value(ctx)
        except PlanError:
            return
        if rate is not None:
            rec["rate_hz"] = rate

    def describe(self, rec):
        return "  %.1f Hz" % rec["rate_hz"] if "rate_hz" in rec else ""


class Save(Comparator):
    keys = ("save",)

    def validate(self, item, where):
        if not is_name(item["save"]):
            raise PlanError("%s: save name must be an identifier" % where)

    def check(self, ctx):
        if not ctx.first or not ctx.first.groups():
            return "save needs a capture group"
        if capture_int(ctx.first) is None:
            return "save capture %r is not an integer" % ctx.first.group(1)
        return None

    def commit(self, ctx):
        ctx.store[ctx.item["save"]] = capture_int(ctx.first)


class SaveMs(Comparator):
    keys = ("save_ms",)
    timed = True

    def validate(self, item, where):
        if not is_name(item["save_ms"]):
            raise PlanError("%s: save_ms name must be an identifier" % where)

    def commit(self, ctx):
        ctx.store[ctx.item["save_ms"]] = ctx.now


COMPARATORS = (
    Count(), CaptureSet(), Range(), Relation(), ElapsedMs(), RateHz(), SaveMs(), Save(),
)
CLAIM_KEYS = SELECTORS + tuple(key for comp in COMPARATORS for key in comp.keys)


def active(item):
    return [comp for comp in COMPARATORS if comp.owns(item)]


def is_timed(item):
    return any(comp.timed for comp in active(item))


def validate_expect(item, where):
    if not isinstance(item, dict):
        raise PlanError("%s: expect entry must be a mapping" % where)
    keys = [k for k in SELECTORS if k in item]
    if len(keys) != 1:
        raise PlanError("%s: expect entry needs one of pattern, absent, equals" % where)
    unknown = sorted(key for key in item if key not in CLAIM_KEYS)
    if unknown:
        raise PlanError("%s: unknown expect key %s" % (where, ", ".join(unknown)))
    comps = active(item)
    if comps and "pattern" not in item:
        names = ", ".join(key for comp in comps for key in comp.keys if key in item)
        raise PlanError("%s: %s need a pattern" % (where, names))
    for comp in comps:
        comp.validate(item, where)


def equals_body(text):
    return PROMPT_LINE_RE.sub("", text or "").strip()


def check_expect(text, expect, store=None, times=None):
    """Return None on success, or a short reason.

    store holds the plan's saved integers and times. save and save_ms write
    to it only after every other check on this entry passed.
    """
    if store is None:
        store = {}
    if "absent" in expect:
        found = re.search(expect["absent"], text, re.M)
        if found:
            return "absent pattern matched: %s" % found.group(0)
        return None
    if "equals" in expect:
        if equals_body(text) != str(expect["equals"]):
            return "equals mismatch"
        return None
    if expect.get("pattern") is None:
        raise PlanError("expect entry needs pattern, absent, or equals")
    ctx = Context(text, expect, store, times)
    if "count" not in expect and not ctx.matches:
        return "pattern did not match"
    comps = active(expect)
    for comp in comps:
        if comp.timed and ctx.now is None:
            return "no byte timestamp"
        reason = comp.check(ctx)
        if reason:
            return reason
    for comp in comps:
        comp.commit(ctx)
    return None


def claim_of(item):
    return {key: item[key] for key in item if key in CLAIM_KEYS}


def claim_text(item):
    return json.dumps(claim_of(item), ensure_ascii=False)


def match_item(text, item, times=None, store=None):
    """Report record for one expect entry: what matched and what was measured."""
    if "absent" in item:
        found = re.search(item["absent"], text or "", re.M)
        rec = {"absent": item["absent"], "hit": bool(found)}
        if found:
            rec["sample"] = found.group(0)[:200]
        return rec
    if "equals" in item:
        return {"equals": str(item["equals"]), "hit": equals_body(text) == str(item["equals"])}
    if not item.get("pattern"):
        return {"pattern": item.get("pattern"), "count": 0}
    ctx = Context(text, item, store if store is not None else {}, times)
    rec = {"pattern": item["pattern"], "count": len(ctx.matches)}
    if ctx.first and ctx.first.groups():
        rec["captures"] = [match.group(1) for match in ctx.matches]
    if ctx.first:
        rec["sample"] = ctx.first.group(0)[:200]
    comps = active(item)
    for comp in comps:
        if not comp.timed:
            comp.report(ctx, rec)
    if ctx.first and ctx.now is not None and any(comp.timed for comp in comps):
        rec["at_ms"] = ctx.now
        if store is not None:
            for comp in comps:
                if comp.timed:
                    comp.report(ctx, rec)
    return rec


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


def describe_item(text, item, times=None, store=None):
    rec = match_item(text, item, times, store)
    if "absent" in rec:
        return 'absent "%s"' % rec["absent"]
    if "equals" in rec:
        return "equals"
    if "captures" in rec and (item.get("count") is not None or "capture_set" in item):
        body = "%d 行 %s  %s" % (rec["count"], compact_span(rec["captures"]), rec.get("sample", ""))
    elif rec.get("sample"):
        body = rec["sample"]
    else:
        body = "无匹配"
    for comp in active(item):
        body += comp.describe(rec)
    return body


def describe_expect(text, expect, times=None, store=None):
    parts = [describe_item(text, item, times, store) for item in expect]
    return " | ".join(part for part in parts if part)
