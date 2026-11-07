"""T12_SPIKE: a month far above the rest of its line."""

from __future__ import annotations

from cre_screen.reconcile.context import CheckContext
from cre_screen.reconcile.metrics import bucket_section, median
from cre_screen.schemas.common import to_cents
from cre_screen.schemas.findings import Finding


def check(ctx: CheckContext) -> list[Finding]:
    if ctx.t12 is None:
        return []
    cfg = ctx.thresholds.t12_spike
    findings: list[Finding] = []
    for ln in ctx.t12.lines:
        if bucket_section(ln.bucket) not in cfg.checked_sections or ln.bucket in cfg.exempt_buckets:
            continue
        mid = median([abs(m) for m in ln.monthly])
        if mid == 0:
            continue
        months = [
            i
            for i, m in enumerate(ln.monthly)
            if abs(m) > cfg.factor * mid and abs(m) - mid > cfg.min_abs_usd
        ]
        if not months:
            continue
        labels = [ctx.t12.month_labels[i] for i in months]
        peak = max(abs(ln.monthly[i]) for i in months)
        findings.append(
            Finding(
                check_id="T12_SPIKE",
                severity="warn",
                message=(
                    f"'{ln.label}' spikes in {', '.join(labels)}: {to_cents(peak)} against a "
                    f"monthly median of {to_cents(mid)}"
                ),
                delta=to_cents(peak - mid),
                line_labels=[ln.label],
                citations=[ln.cell_citations.get(f"m{i + 1:02d}", ln.citation) for i in months],
                details={"months": ",".join(labels), "median": str(to_cents(mid))},
            )
        )
    return findings
