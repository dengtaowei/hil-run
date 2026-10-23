import copy

import pytest

from hilrun.errors import PlanError
from hilrun.plan import expand_clock_specs, expand_refs, iter_steps, prepare_plan, validate_step

DEVICES = {"ap": "board_ap", "hp": "board_hp"}
BENCH = {"devices": DEVICES, "clocks": {"rtc": {"hz": 32768, "tol_pct": 5}}}


def step(**kw):
    out = {"device": "ap", "write": "x"}
    out.update(kw)
    return out


def test_expand_refs():
    assert expand_refs("chiptest_timer get_count ${c0}", {"c0": 10}) == "chiptest_timer get_count 10"
    with pytest.raises(PlanError):
        expand_refs("chiptest_timer get_count ${missing}", {})


def test_validate_accepts():
    validate_step(step(expect=[{"pattern": r"^rtc0 tick (\d+)$", "gt": "n1",
                                "rate_hz": {"tick": "n1", "time": "t1", "min": 30000, "max": 36000}}]),
                  DEVICES, "rate")
    validate_step({"device": "ap", "read": True, "expect": [{"pattern": "x"}]}, DEVICES, "read")
    validate_step({"device": "ap", "sleep_ms": 1050, "expect": [{"pattern": "x"}]}, DEVICES, "sleep")


@pytest.mark.parametrize("bad, message", [
    (step(expect=[{"pattern": r"(\d+)", "gt": "c0", "eq": 1}]), "use one of"),
    ({"device": "ap", "sleep_ms": 1, "write": "x", "expect": [{"pattern": "x"}]}, "exactly one"),
    ({"device": "ap", "sleep_ms": -1, "expect": [{"pattern": "x"}]}, "non-negative"),
    ({"device": "ap", "read": True, "write": "x", "expect": [{"pattern": "x"}]}, "exactly one"),
    ({"device": "ap", "read": True}, "needs expect"),
    ({"device": "ap", "read": True, "mutating": True, "expect": [{"pattern": "x"}]}, "cannot be mutating"),
    (step(device="zz"), "unknown device"),
    (step(expct=[]), "unknown step key expct"),
    (step(expect={"pattern": "x"}), "must be a list"),
])
def test_validate_rejects(bad, message):
    with pytest.raises(PlanError, match=message):
        validate_step(bad, DEVICES, "w")


def test_parallel_rules():
    good = [{"parallel": [step(id="a"), step(id="b", device="hp")]}]
    assert len(list(iter_steps(good, DEVICES, "p"))) == 1
    with pytest.raises(PlanError, match="appears twice"):
        list(iter_steps([{"parallel": [step(), step()]}], DEVICES, "p"))
    with pytest.raises(PlanError, match="mutating"):
        list(iter_steps([{"parallel": [step(mutating=True)]}], DEVICES, "p"))
    with pytest.raises(PlanError, match="empty"):
        list(iter_steps([{"parallel": []}], DEVICES, "p"))
    with pytest.raises(PlanError, match="only the parallel key"):
        list(iter_steps([{"parallel": [step()], "id": "g"}], DEVICES, "p"))


def test_clock_expansion():
    plan = [step(expect=[
        {"pattern": r"(\d+)", "rate_hz": {"tick": "n1", "time": "t1", "clock": "rtc"}},
        {"pattern": r"(\d+)", "range": {"clock": "rtc"}},
        {"pattern": r"(\d+)", "range": {"clock": "rtc", "tol_pct": 10}},
        {"pattern": r"(\d+)", "range": {"hz": 1000, "tol_pct": 1}},
    ])]
    expand_clock_specs(plan, BENCH["clocks"], "c")
    expect = plan[0]["expect"]
    assert expect[0]["rate_hz"] == {"tick": "n1", "time": "t1", "hz": 32768, "tol_pct": 5}
    assert expect[1]["range"] == {"min": 32768 * 0.95, "max": 32768 * 1.05}
    assert expect[2]["range"]["min"] == pytest.approx(32768 * 0.9)
    assert expect[3]["range"] == {"min": 990.0, "max": 1010.0}


def test_unknown_clock():
    with pytest.raises(PlanError, match="unknown clock"):
        expand_clock_specs([{"expect": [{"rate_hz": {"tick": 1, "time": "t", "clock": "missing"}}]}], {}, "x")


def test_prepare_plan_leaves_input_alone():
    plan = {"name": "p", "steps": [step(expect=[
        {"pattern": r"(\d+)", "rate_hz": {"tick": 0, "time": "t", "clock": "rtc"}}])]}
    before = copy.deepcopy(plan)
    ready = prepare_plan(plan, BENCH)
    assert plan == before
    assert ready["steps"][0]["expect"][0]["rate_hz"]["hz"] == 32768
    assert ready["cleanup"] == []


def test_prepare_plan_rejects_unknown_plan_key():
    with pytest.raises(PlanError, match="unknown plan key clenaup"):
        prepare_plan({"name": "p", "steps": [], "clenaup": []}, BENCH)
