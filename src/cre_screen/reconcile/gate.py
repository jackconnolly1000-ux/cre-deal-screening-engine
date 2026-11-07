"""The reconciliation gate: FOOTING -> (REEXTRACT) -> CHECKS -> REVIEW or READY.

Deterministic Python. No I/O and no LLM calls: the re-reader is injected.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from cre_screen.errors import OverrideError
from cre_screen.reconcile.checks import REGISTRY
from cre_screen.reconcile.context import CheckContext
from cre_screen.reconcile.reread import Rereader, localize
from cre_screen.schemas.findings import Finding, GateState, GateTransition, Override

logger = logging.getLogger(__name__)

APPLIED_BY_UI = ("edit_value", "remap_line", "resolve_footing")
STATUS_FOR_ACTION = {"clear": "cleared", "override": "overridden"}


@dataclass
class GateResult:
    findings: list[Finding]
    gate_log: list[GateTransition]
    final_state: GateState


def _targets(finding: Finding, target: str) -> bool:
    return target == "*" or target in finding.unit_ids or target in finding.line_labels


def apply_overrides(
    findings: list[Finding], overrides: Sequence[Override], min_note_chars: int
) -> None:
    """Mark findings cleared/overridden. An override whose finding no longer exists is ignored."""
    for override in overrides:
        if override.action in APPLIED_BY_UI:
            raise OverrideError(
                f"'{override.action}' changes extracted data and is applied by the review UI, "
                "which then re-runs the gate from FOOTING"
            )
        if len(override.note.strip()) < min_note_chars:
            raise OverrideError(f"override notes need at least {min_note_chars} characters")
        for finding in findings:
            if finding.check_id == override.check_id and _targets(finding, override.target):
                finding.status = STATUS_FOR_ACTION[override.action]  # type: ignore[assignment]
                finding.reviewer_note = override.note.strip()


def run_gate(
    ctx: CheckContext,
    *,
    reread: Rereader | None = None,
    overrides: Sequence[Override] = (),
    now: Callable[[], datetime] | None = None,
) -> GateResult:
    clock = now or (lambda: datetime.now(UTC))
    log: list[GateTransition] = []
    state: GateState = "FOOTING"

    def move(to: GateState, reason: str) -> None:
        nonlocal state
        log.append(GateTransition(from_state=state, to_state=to, reason=reason, at=clock()))
        state = to

    cfg = ctx.thresholds.rr_footing
    footing = REGISTRY["RR_FOOTING"](ctx)
    failing = [
        f for f in footing if f.severity == "block" and f.details.get("column") == "in_place_rent"
    ]

    attempts = 0
    if not failing:
        verifiable = any(f.severity != "info" for f in footing) or bool(
            ctx.rent_roll.stated_totals.get("in_place_rent")  # type: ignore[call-overload]
        )
        move("CHECKS", "in-place rent foots" if verifiable else "footing not verifiable")
    elif attempts < cfg.max_reextract_attempts and reread is not None:
        attempts += 1
        move("REEXTRACT", f"{len(failing)} in-place rent footing failure(s); re-reading")
        block = localize(ctx.rent_roll, cfg.tolerance_usd)
        result = reread(block)
        for finding in failing:
            if result.agrees:
                finding.details["cause"] = "document"
            else:
                finding.details["cause"] = "extraction"
                finding.unit_ids = list(result.disagreeing_unit_ids)
        reason = (
            "re-read agrees with the first read: the document itself does not foot"
            if result.agrees
            else f"re-read disagrees on {len(result.disagreeing_unit_ids)} unit(s): extraction"
        )
        move("CHECKS", reason)
    else:
        for finding in failing:
            finding.details["cause"] = "unverified"
        move("CHECKS", "footing failed; no re-read available")

    findings = list(footing)
    for check_id, check in REGISTRY.items():
        if check_id == "RR_FOOTING":
            continue
        findings.extend(check(ctx))

    apply_overrides(findings, overrides, ctx.thresholds.review.override_note_min_chars)
    open_blocks = [f for f in findings if f.severity == "block" and f.status == "open"]
    if open_blocks:
        move("REVIEW", f"{len(open_blocks)} open block finding(s)")
    else:
        move("READY", "no open block findings")
    return GateResult(findings=findings, gate_log=log, final_state=state)
