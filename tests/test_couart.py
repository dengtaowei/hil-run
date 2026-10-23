import pytest

from hilrun.couart import normalize_timed, parse_historyt, slice_new
from hilrun.errors import PlanError


def test_parse_historyt_spans():
    reply = "OK bytes=8 seq=100 spans=2\n%s\n4 1000000000\n4 1005000000\n" % b"AAA\nBBB\n".hex()
    seq, text, times = parse_historyt(reply)
    assert seq == 100
    assert text == "AAA\nBBB\n"
    assert times == [1000] * 4 + [1005] * 4


@pytest.mark.parametrize("reply", [
    "ERR oom\n",
    "OK bytes=2 seq=1\n4141\n",
    "OK bytes=2 seq=1 spans=1\n414\n2 1\n",
    "OK bytes=3 seq=1 spans=1\n4141\n2 1\n",
    "OK bytes=2 seq=1 spans=1\n4141\n1 1\n",
    "OK bytes=2 seq=1 spans=1\n4141\nbad\n",
])
def test_parse_historyt_rejects(reply):
    with pytest.raises(PlanError):
        parse_historyt(reply)


def test_slice_new():
    assert slice_new(10, 13, "abcdef", [1, 2, 3, 4, 5, 6]) == ("def", [4, 5, 6])
    assert slice_new(10, 10, "abc", [1, 2, 3]) == ("", [])
    assert slice_new(0, 99, "abc", None) == ("abc", None)


def test_normalize_timed_keeps_lf_stamp():
    text, times = normalize_timed("a\r\nb\rc", [1, 2, 3, 4, 5, 6])
    assert text == "a\nb\nc"
    assert times == [1, 3, 4, 5, 6]


def test_normalize_timed_length_mismatch_drops_stamps():
    assert normalize_timed("a\r\n", [1]) == ("a\n", None)
