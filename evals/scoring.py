"""Score one extracted-and-checked deal against its ground truth (SPEC 11.4).

Truth is the document view: values exactly as printed, traps included. Money is Decimal.
"""

from __future__ import annotations

from decimal import Decimal

from cre_screen.reconcile.metrics import normalize_unit_id
from cre_screen.schemas.deal import DealPackage, DealScore, DealTruth, TruthUnit
from cre_screen.schemas.findings import ExpectedFinding, Finding
from cre_screen.schemas.rent_roll import RentRollUnit

NUMERIC_FIELDS = (
    "beds",
    "baths",
    "sq_ft",
    "in_place_rent",
    "market_rent",
    "lease_start",
    "lease_end",
)
DEFAULT_PROVENANCE = {"pdf_text": "text_layer", "xlsx": "excel", "pdf_scanned": "llm_vision"}
ZERO = Decimal("0")


def finding_matches(expected: ExpectedFinding, finding: Finding) -> bool:
    """Same check id, and either nothing specific is expected or the ids/labels overlap."""
    if finding.check_id != expected.check_id:
        return False
    if not expected.unit_ids and not expected.line_labels:
        return True
    wanted_units = {normalize_unit_id(u) for u in expected.unit_ids}
    got_units = {normalize_unit_id(u) for u in finding.unit_ids}
    return bool(wanted_units & got_units) or bool(
        set(expected.line_labels) & set(finding.line_labels)
    )


def _unique_truth(units: list[TruthUnit]) -> dict[str, TruthUnit]:
    out: dict[str, TruthUnit] = {}
    for unit in units:
        out.setdefault(normalize_unit_id(unit.unit_id), unit)
    return out


def _unique_extracted(units: list[RentRollUnit]) -> dict[str, RentRollUnit]:
    out: dict[str, RentRollUnit] = {}
    for unit in units:
        out.setdefault(normalize_unit_id(unit.unit_id), unit)
    return out


def _miss_reason(expected: ExpectedFinding, findings: list[Finding]) -> str:
    same_check = [f for f in findings if f.check_id == expected.check_id]
    if not same_check:
        return f"{expected.check_id} raised no finding"
    target = ", ".join([*expected.unit_ids, *expected.line_labels])
    return f"{expected.check_id} raised findings that do not name {target}"


def score_deal(pkg: DealPackage, truth: DealTruth) -> DealScore:
    truth_units = _unique_truth(truth.units)
    got_units = _unique_extracted(pkg.rent_roll.units)
    default_prov = DEFAULT_PROVENANCE[pkg.rent_roll.source_format]

    total = correct = 0
    mix: dict[str, int] = {}
    for key, want in truth_units.items():
        got = got_units.get(key)
        for name in NUMERIC_FIELDS:
            wanted = getattr(want, name)
            if wanted is None:
                continue
            cite = None if got is None else got.citations.get(name)
            prov = cite.provenance if cite is not None else default_prov
            mix[prov] = mix.get(prov, 0) + 1
            total += 1
            if got is not None and getattr(got, name) == wanted:
                correct += 1

    coa_total = coa_correct = ZERO
    extracted_lines = {} if pkg.t12 is None else {ln.label: ln for ln in pkg.t12.lines}
    for want_line in truth.t12_lines:
        weight = abs(want_line.stated_total)
        coa_total += weight
        got_line = extracted_lines.get(want_line.label)
        if got_line is not None and got_line.bucket == want_line.true_bucket:
            coa_correct += weight

    misses: list[str] = []
    caught = 0
    for expected in truth.expected:
        if any(finding_matches(expected, f) for f in pkg.findings):
            caught += 1
        else:
            misses.append(f"{expected.trap_id}: {_miss_reason(expected, pkg.findings)}")

    raised = [f for f in pkg.findings if f.severity in ("warn", "block")]
    false_positives = sum(
        1 for f in raised if not any(finding_matches(e, f) for e in truth.expected)
    )
    return DealScore(
        deal_id=truth.deal_id,
        split=truth.split,
        provenance_mix=mix,
        numeric_fields_total=total,
        numeric_fields_correct=correct,
        units_in_doc=len(truth_units),
        units_found=sum(1 for key in truth_units if key in got_units),
        phantom_units=sum(1 for key in got_units if key not in truth_units),
        coa_dollars_total=coa_total,
        coa_dollars_correct=coa_correct,
        planted=len(truth.expected),
        caught=caught,
        false_positives=false_positives,
        clean_control_findings=len(raised) if not truth.expected else 0,
        cost_usd=pkg.cost_usd,
        wall_seconds=pkg.wall_seconds,
        misses=misses,
    )


def failed_score(truth: DealTruth, reason: str) -> DealScore:
    """A deal the pipeline could not process: every truth item counts as missed."""
    truth_units = _unique_truth(truth.units)
    total = sum(
        1 for u in truth_units.values() for n in NUMERIC_FIELDS if getattr(u, n) is not None
    )
    return DealScore(
        deal_id=truth.deal_id,
        split=truth.split,
        provenance_mix={},
        numeric_fields_total=total,
        numeric_fields_correct=0,
        units_in_doc=len(truth_units),
        units_found=0,
        phantom_units=0,
        coa_dollars_total=sum((abs(ln.stated_total) for ln in truth.t12_lines), ZERO),
        coa_dollars_correct=ZERO,
        planted=len(truth.expected),
        caught=0,
        false_positives=0,
        clean_control_findings=0,
        cost_usd=ZERO,
        wall_seconds=0.0,
        misses=[f"{e.trap_id}: deal failed to process ({reason})" for e in truth.expected]
        or [f"{truth.deal_id}: deal failed to process ({reason})"],
    )
