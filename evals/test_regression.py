"""Dev-split regression guard (SPEC 11.7).

Loads evals/baseline_dev.json, runs the dev split in replay mode, and fails if the caught count
drops, false positives rise, or numeric accuracy drops by more than 0.5 percentage points.
Re-baselining is the owner's call: ``python -m evals.run_eval --split dev --update-baseline``.
"""

from __future__ import annotations

import json
from decimal import Decimal

from cre_screen.config import load_config
from cre_screen.runtime import with_overrides
from evals.report import aggregate
from evals.run_eval import BASELINE_PATH, evaluate

TOLERANCE = Decimal("0.005")


def test_dev_split_has_not_regressed_against_the_baseline() -> None:
    assert BASELINE_PATH.exists(), "evals/baseline_dev.json is missing; the owner records it"
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    cfg = with_overrides(load_config(), cache_mode="replay")
    scores, cost = evaluate(cfg)
    overall = aggregate(scores)["overall"]

    assert cost == 0, "replay mode must not spend"
    assert overall["caught"] >= baseline["caught"], "fewer planted errors caught than baseline"
    assert overall["false_positives"] <= baseline["false_positives"], "more false positives"
    assert Decimal(overall["numeric_accuracy"]) >= Decimal(baseline["numeric_accuracy"]) - TOLERANCE
    # Clean controls must stay clean regardless of the baseline.
    assert all(s.clean_control_findings == 0 for s in scores if s.planted == 0)
