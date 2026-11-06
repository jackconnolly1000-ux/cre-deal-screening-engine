"""T12_FOOTING: each line's months must sum to its stated total, and stated subtotals must foot."""

from __future__ import annotations

from decimal import Decimal

from cre_screen.config import ThresholdsConfig
from cre_screen.reconcile.context import CheckContext
from cre_screen.schemas.common import OPEX_BUCKETS, REVENUE_BUCKETS, to_cents
from cre_screen.schemas.findings import Finding
from cre_screen.schemas.t12 import StatedSubtotal, T12Line, T12Statement

ZERO = Decimal("0")


def _column_sum(lines: list[T12Line], buckets: tuple[str, ...], month: int | None) -> Decimal:
    total = ZERO
    for ln in lines:
        if ln.bucket in buckets:
            total += ln.computed_total() if month is None else ln.monthly[month]
    return total


def _expected(
    sub: StatedSubtotal,
    t12: T12Statement,
    stated: dict[str, StatedSubtotal],
    month: int | None,
) -> Decimal | None:
    """What a stated subtotal should equal at a month (or for the total when month is None)."""
    revenue = _column_sum(t12.lines, REVENUE_BUCKETS, month)
    opex = _column_sum(t12.lines, OPEX_BUCKETS, month)
    if sub.kind == "total_income":
        return revenue
    if sub.kind == "total_expenses":
        return opex
    if sub.kind == "noi":
        income = stated.get("total_income")
        expenses = stated.get("total_expenses")
        if income is not None and expenses is not None:
            i = income.total if month is None else income.monthly[month]
            e = expenses.total if month is None else expenses.monthly[month]
            if i is not None and e is not None:
                return i - e
        return revenue - opex
    return None


def footing_findings(t12: T12Statement, thresholds: ThresholdsConfig) -> list[Finding]:
    cfg = thresholds.t12_footing
    findings: list[Finding] = []

    for ln in t12.lines:
        if ln.stated_total is None:
            continue
        delta = ln.computed_total() - ln.stated_total
        if abs(delta) > cfg.line_tolerance_usd:
            findings.append(
                Finding(
                    check_id="T12_FOOTING",
                    severity="block",
                    message=(
                        f"'{ln.label}' months sum to {ln.computed_total()} but the printed "
                        f"total is {ln.stated_total}"
                    ),
                    delta=to_cents(delta),
                    line_labels=[ln.label],
                    citations=[ln.citation],
                    details={
                        "kind": "line",
                        "computed": str(ln.computed_total()),
                        "stated": str(ln.stated_total),
                    },
                )
            )

    stated = {s.kind: s for s in t12.stated_subtotals if s.kind != "other"}
    for sub in t12.stated_subtotals:
        failures: list[str] = []
        worst = ZERO
        expected_total = _expected(sub, t12, stated, None)
        if sub.total is not None and expected_total is not None:
            diff = sub.total - expected_total
            if abs(diff) > cfg.subtotal_tolerance_usd:
                failures.append("total")
                worst = diff
        for month in range(12):
            value = sub.monthly[month]
            expected = _expected(sub, t12, stated, month)
            if value is None or expected is None:
                continue
            diff = value - expected
            if abs(diff) > cfg.subtotal_tolerance_usd:
                failures.append(t12.month_labels[month])
                if abs(diff) > abs(worst):
                    worst = diff
        if failures:
            findings.append(
                Finding(
                    check_id="T12_FOOTING",
                    severity="block",
                    message=(
                        f"stated subtotal '{sub.label}' does not foot against the lines "
                        f"({', '.join(failures)})"
                    ),
                    delta=to_cents(worst),
                    line_labels=[sub.label],
                    citations=[sub.citation],
                    details={
                        "kind": "subtotal",
                        "subtotal_kind": sub.kind,
                        "failed": ",".join(failures),
                    },
                )
            )
    return findings


def check(ctx: CheckContext) -> list[Finding]:
    if ctx.t12 is None:
        return []
    return footing_findings(ctx.t12, ctx.thresholds)
