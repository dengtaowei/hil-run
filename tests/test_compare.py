import pytest

from hilrun.compare import (
    COMPARATORS, check_expect, claim_of, describe_item, match_item, validate_expect,
)
from hilrun.errors import PlanError

OWNED = "\n".join("channel %d : used : 1 owner id : 0" % i for i in range(16)) + "\nboot> "


def test_count_and_capture_set():
    assert check_expect(OWNED, {
        "pattern": r"^channel (\d+) : used : 1 owner id : 0$",
        "count": 16,
        "capture_set": "0..15",
    }) is None


def test_count_short():
    short = "\n".join("channel %d : used : 0" % i for i in range(15))
    reason = check_expect(short, {
        "pattern": r"^channel (\d+) : used : 0$", "count": 16, "capture_set": "0..15",
    })
    assert reason and "matched 15" in reason


def test_capture_set_duplicate():
    dup = OWNED.replace("boot>", "channel 0 : used : 1 owner id : 0\nboot>")
    reason = check_expect(dup, {
        "pattern": r"^channel (\d+) : used : 1 owner id : 0$", "capture_set": "0..15",
    })
    assert reason and "capture set" in reason


def test_absent():
    assert check_expect("ipcc init failed\n", {"absent": "ipcc init failed"})
    assert check_expect("ok\n", {"absent": "ipcc init failed"}) is None


def test_equals_strips_prompt_lines():
    assert check_expect("version\nversion 1\nboot> ", {"equals": "version\nversion 1"}) is None
    assert check_expect("version 2\nboot> ", {"equals": "version 1"}) == "equals mismatch"


def test_range():
    item = {"pattern": r"temp=(\d+)", "range": {"min": 20, "max": 30}}
    assert check_expect("temp=25C\n", item) is None
    assert check_expect("temp=99C\n", item)


def test_save_and_relations():
    store = {}
    assert check_expect("timer 0 count = 10\n", {
        "pattern": r"^timer 0 count = (\d+)$", "save": "c0"}, store) is None
    assert store["c0"] == 10
    assert check_expect("timer 0 count = 25\n", {
        "pattern": r"^timer 0 count = (\d+)$", "gt": "c0"}, store) is None
    assert check_expect("timer 0 count = 10\n", {
        "pattern": r"^timer 0 count = (\d+)$", "gt": "c0"}, store)
    assert check_expect("timer 3 count = 0\n", {
        "pattern": r"^timer 3 count = (\d+)$", "eq": 0}, store) is None


def test_save_waits_for_other_checks():
    store = {}
    reason = check_expect("n=5\n", {"pattern": r"n=(\d+)", "gt": 10, "save": "n"}, store)
    assert reason and "not gt" in reason
    assert "n" not in store


def test_unknown_saved_name_is_plan_error():
    with pytest.raises(PlanError):
        check_expect("n=5\n", {"pattern": r"n=(\d+)", "gt": "missing"}, {})


ARMED = "rtc0 alarm set for 1 seconds\n"
FIRED = "RTC interrupt triggered!\n"
BOTH = ARMED + FIRED
STAMPS = [1000] * len(ARMED) + [2000] * len(FIRED)


def armed_store():
    store = {}
    assert check_expect(BOTH, {
        "pattern": r"^rtc0 alarm set for 1 seconds$", "save_ms": "armed"}, store, STAMPS) is None
    assert store["armed"] == 1000
    return store


def test_elapsed_absolute():
    store = armed_store()
    item = {"pattern": r"^RTC interrupt triggered!$"}
    assert check_expect(BOTH, dict(item, elapsed_ms={"since": "armed", "min": 900, "max": 1200}),
                        store, STAMPS) is None
    assert check_expect(BOTH, dict(item, elapsed_ms={"since": "armed", "min": 1201, "max": 1300}),
                        store, STAMPS)


def test_elapsed_tolerance():
    store = armed_store()
    item = {"pattern": r"^RTC interrupt triggered!$",
            "elapsed_ms": {"since": "armed", "ms": 1000, "tol_pct": 5}}
    assert check_expect(BOTH, item, store, STAMPS) is None
    late = [1000] * len(ARMED) + [1060] * len(FIRED)
    assert "outside" in check_expect(BOTH, item, store, late)


TICKS = "rtc0 tick 1000\nrtc0 tick 4277\n"
TICK_TIMES = [0] * len("rtc0 tick 1000\n") + [100] * len("rtc0 tick 4277\n")
SECOND = "rtc0 tick 4277"
SECOND_TIMES = [100] * len(SECOND)


def rate_store():
    store = {}
    assert check_expect(TICKS, {
        "pattern": r"^rtc0 tick (\d+)$", "save": "n1", "save_ms": "t1"}, store, TICK_TIMES) is None
    return store


def test_rate_counts_first_match():
    store = rate_store()
    assert check_expect(TICKS, {
        "pattern": r"^rtc0 tick (\d+)$", "count": 2,
        "rate_hz": {"tick": "n1", "time": "t1", "min": 30000, "max": 36000},
    }, store, TICK_TIMES)


def test_rate_absolute_and_tolerance():
    store = rate_store()
    base = {"pattern": r"^rtc0 tick (\d+)$"}
    assert check_expect(SECOND, dict(base, rate_hz={
        "tick": "n1", "time": "t1", "min": 30000, "max": 36000}), store, SECOND_TIMES) is None
    assert check_expect(SECOND, dict(base, rate_hz={
        "tick": "n1", "time": "t1", "hz": 32768, "tol_pct": 5}), store, SECOND_TIMES) is None
    assert check_expect(SECOND, dict(base, rate_hz={
        "tick": "n1", "time": "t1", "hz": 32768, "tol_pct": 0.001}), store, SECOND_TIMES)


def test_rate_integer_tick():
    store = rate_store()
    assert check_expect(SECOND, {"pattern": r"^rtc0 tick (\d+)$", "rate_hz": {
        "tick": 1000, "time": "t1", "hz": 32768, "tol_pct": 5}}, store, SECOND_TIMES) is None


def test_rate_zero_interval():
    store = rate_store()
    reason = check_expect(SECOND, {"pattern": r"^rtc0 tick (\d+)$", "rate_hz": {
        "tick": "n1", "time": "t1", "min": 0, "max": 1}}, store, [0] * len(SECOND))
    assert reason == "rate interval 0 ms"


def test_timed_needs_stamps():
    reason = check_expect("rtc0 tick 5\n", {"pattern": r"tick (\d+)", "save_ms": "t"}, {}, None)
    assert reason == "no byte timestamp"


def test_report_and_verdict_share_rate():
    store = rate_store()
    item = {"pattern": r"^rtc0 tick (\d+)$",
            "rate_hz": {"tick": "n1", "time": "t1", "hz": 32768, "tol_pct": 5}}
    rec = match_item(SECOND, item, SECOND_TIMES, store)
    assert rec["at_ms"] == 100
    assert rec["rate_hz"] == pytest.approx((4277 - 1000) * 10.0)
    assert describe_item(SECOND, item, SECOND_TIMES, store).endswith("32770.0 Hz")


def test_match_record_fields():
    item = {"pattern": r"^channel (\d+) : used : 1 owner id : 0$", "count": 16, "capture_set": "0..15"}
    rec = match_item(OWNED, item)
    assert rec["count"] == 16 and rec["want"] == 16 and rec["capture_set"] == "0..15"
    assert describe_item(OWNED, item).startswith("16 行 0..15")


def test_claim_of_drops_unknown():
    assert claim_of({"pattern": "x", "note": "y", "count": 1}) == {"pattern": "x", "count": 1}


@pytest.mark.parametrize("item, message", [
    ({"count": 1}, "needs one of pattern"),
    ({"pattern": "x", "absent": "y"}, "needs one of pattern"),
    ({"pattern": "x", "rate_hzz": {}}, "unknown expect key rate_hzz"),
    ({"absent": "x", "save": "n"}, "need a pattern"),
    ({"pattern": "x", "gt": 1, "eq": 2}, "use one of"),
    ({"pattern": "x", "gt": 1.5}, "integer or a saved name"),
    ({"pattern": "x", "save": "1bad"}, "identifier"),
    ({"pattern": "x", "save_ms": "1bad"}, "identifier"),
    ({"pattern": "x", "range": [1, 2]}, "range must be"),
    ({"pattern": "x", "elapsed_ms": {"min": 1, "max": 2}}, "since"),
    ({"pattern": "x", "elapsed_ms": {"since": "a", "min": 1, "ms": 2, "tol_pct": 1}}, "elapsed_ms needs"),
    ({"pattern": "x", "rate_hz": {"tick": "n", "time": "t"}}, "rate_hz needs"),
    ({"pattern": "x", "rate_hz": {"tick": "n", "time": "t", "hz": 1, "tol_pct": -1}}, "rate_hz needs"),
    ({"pattern": "x", "rate_hz": {"tick": True, "time": "t", "min": 1, "max": 2}}, "tick and time"),
])
def test_validate_rejects(item, message):
    with pytest.raises(PlanError, match=message):
        validate_expect(item, "w")


def test_validate_accepts_every_comparator():
    validate_expect({
        "pattern": r"(\d+)", "count": 1, "capture_set": "1..1", "range": {"min": 0, "max": 9},
        "gt": "n0", "save": "n1", "save_ms": "t1",
        "elapsed_ms": {"since": "t0", "ms": 10, "tol_pct": 5},
        "rate_hz": {"tick": "n0", "time": "t0", "hz": 1, "tol_pct": 5},
    }, "w")


def test_comparator_keys_are_unique():
    keys = [key for comp in COMPARATORS for key in comp.keys]
    assert len(keys) == len(set(keys))
