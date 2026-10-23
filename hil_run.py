#!/usr/bin/env python3
"""Run serial plans against couart hubs and print a JSON verdict.

Entry point kept at this path so existing invocations keep working:

    python3 ~/hil-run/hil_run.py --bench tests/hil/bench.yaml tests/hil/rtc

Plan format, comparators, and report layout are in README.md. Code is in
hilrun/. Exit code 0 passes, 1 fails.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hilrun.cli import entry, main  # noqa: E402,F401
from hilrun.compare import check_expect, claim_of, match_item  # noqa: E402,F401
from hilrun.couart import history, history_timed, parse_historyt  # noqa: E402,F401
from hilrun.errors import PlanError, StepFailure  # noqa: E402,F401
from hilrun.plan import (  # noqa: E402,F401
    expand_clock_specs, iter_steps, load_yaml, prepare_plan, validate_step,
)
from hilrun.report import verify_report  # noqa: E402,F401

__all__ = [
    "PlanError", "StepFailure", "check_expect", "claim_of", "entry", "expand_clock_specs",
    "history", "history_timed", "iter_steps", "load_yaml", "main", "match_item",
    "parse_historyt", "prepare_plan", "validate_step", "verify_report",
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    sys.exit(entry(sys.argv[1:], ROOT))
