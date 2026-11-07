"""Human review: overrides that change extracted data, then a fresh gate run (SPEC 8.4).

``clear`` and ``override`` only change a finding's status, and the gate applies them itself.
``edit_value``, ``remap_line`` and ``resolve_footing`` change the extracted data, so they are
applied here, before the gate, which then runs again from FOOTING. The number a reviewer chose
travels in ``Override.corrected_value``; ``target`` says where it goes:

    edit_value       "<unit id>:<field>"        e.g. "101:in_place_rent"
    remap_line       "<line label>=<BUCKET>"    e.g. "Gas=UTILITIES"
    resolve_footing  "<stated total column>"    e.g. "in_place_rent"

``apply_data_overrides`` is idempotent, so the Excel writer can apply it again as a safety net.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import get_args

from cre_screen.config import AppConfig
from cre_screen.errors import OverrideError
from cre_screen.ingest.reread import make_rereader
from cre_screen.reconcile.context import CheckContext
from cre_screen.reconcile.gate import STRICT_MATH_CHECKS, run_gate
from cre_screen.reconcile.metrics import normalize_unit_id
from cre_screen.schemas.common import COABucket, to_cents
from cre_screen.schemas.deal import DealPackage
from cre_screen.schemas.findings import Override
from cre_screen.schemas.rent_roll import RentRoll
from cre_screen.schemas.t12 import T12Statement

EDIT_FIELDS = ("in_place_rent", "market_rent")
BUCKETS: tuple[str, ...] = get_args(COABucket)
STATUS_ACTIONS = ("clear", "override")
STATED_COLUMNS = ("in_place_rent", "market_rent")


def remap_target(label: str, bucket: str) -> str:
    return f"{label}={bucket}"


def edit_target(unit_id: str, field: str) -> str:
    return f"{unit_id}:{field}"


def parse_remap(target: str) -> tuple[str, str]:
    label, sep, bucket = target.rpartition("=")
    if not sep or not label or bucket not in BUCKETS:
        raise OverrideError(f"remap_line target must be '<line label>=<BUCKET>' (got {target!r})")
    return label, bucket


def parse_edit(target: str) -> tuple[str, str]:
    unit_id, colon, field = target.rpartition(":")
    if not (colon and unit_id) or field not in EDIT_FIELDS:
        raise OverrideError(
            f"edit_value target must be '<unit id>:<{'|'.join(EDIT_FIELDS)}>' (got {target!r})"
        )
    return unit_id, field


ALLOWED_STRATEGIES = {
    "resolve_footing": ("use_calculated_sum",),
    "edit_value": ("use_stated_total", "edit_line_item"),
}


def require_strategy(override: Override) -> None:
    """A data override aimed at a strict-math check must say which number governs."""
    if override.check_id not in STRICT_MATH_CHECKS:
        return
    allowed = ALLOWED_STRATEGIES.get(override.action, ())
    if override.resolution_strategy not in allowed:
        raise OverrideError(
            f"{override.check_id} {override.action} needs a resolution_strategy "
            f"({' or '.join(allowed)}), got {override.resolution_strategy!r}"
        )


def money_value(override: Override) -> Decimal:
    """The reviewer's number. Data overrides without one are rejected."""
    if override.corrected_value is None:
        raise OverrideError(f"{override.action} needs a corrected_value")
    return override.corrected_value


def calculated_sum(rent_roll: RentRoll, column: str) -> Decimal:
    """Sum of a money column over every unit row, to the cent."""
    return to_cents(sum((getattr(u, column) or Decimal(0) for u in rent_roll.units), Decimal(0)))


def status_overrides(overrides: Sequence[Override]) -> list[Override]:
    """The overrides the gate itself applies (they only change a finding's status)."""
    return [o for o in overrides if o.action in STATUS_ACTIONS]


def _remap(t12: T12Statement | None, label: str, bucket: str) -> T12Statement:
    if t12 is None or label not in {ln.label for ln in t12.lines}:
        raise OverrideError(f"remap_line: no T-12 line labelled {label!r}")
    lines = []
    for ln in t12.lines:
        done = ln.bucket == bucket and ln.mapping_source == "manual_override"
        if ln.label != label or done:
            lines.append(ln)
            continue
        lines.append(
            ln.model_copy(
                update={
                    "bucket": bucket,
                    "mapping_source": "manual_override",
                    "mapping_confidence": 1.0,
                    "audit_flags": [*ln.audit_flags, "remapped_by_reviewer"],
                }
            )
        )
    return t12.model_copy(update={"lines": lines})


def _edit(rent_roll: RentRoll, unit_id: str, field: str, value: Decimal) -> RentRoll:
    wanted = normalize_unit_id(unit_id)
    index = next(
        (i for i, u in enumerate(rent_roll.units) if normalize_unit_id(u.unit_id) == wanted), None
    )
    if index is None:
        raise OverrideError(f"edit_value: no unit {unit_id!r} in the rent roll")
    unit = rent_roll.units[index]
    cited = unit.citations.get(field)
    if (
        getattr(unit, field) == value
        and cited is not None
        and cited.provenance == "manual_override"
    ):
        return rent_roll  # already applied
    origin = cited or unit.citations["unit_id"].model_copy(update={"raw_text": ""})
    flag = f"edited_{field}"
    edited = unit.model_copy(
        update={
            field: value,
            "citations": {
                **unit.citations,
                field: origin.model_copy(update={"provenance": "manual_override"}),
            },
            "audit_flags": unit.audit_flags
            if flag in unit.audit_flags
            else [*unit.audit_flags, flag],
        }
    )
    units = list(rent_roll.units)
    units[index] = edited
    return rent_roll.model_copy(update={"units": units})


def _resolve_footing(rent_roll: RentRoll, column: str, value: Decimal) -> RentRoll:
    """Replace a stated grand total by the computed sum, which the reviewer says is right."""
    if column not in STATED_COLUMNS or column not in rent_roll.stated_totals:
        raise OverrideError(f"resolve_footing: the rent roll has no stated {column!r} total")
    computed = calculated_sum(rent_roll, column)
    cited = rent_roll.stated_total_citations[column]  # type: ignore[index]
    if cited.provenance == "manual_override" and rent_roll.stated_totals[column] == value:  # type: ignore[index]
        return rent_roll  # already applied
    if value != computed:
        raise OverrideError(
            f"resolve_footing: corrected_value {value} is not the calculated sum {computed}"
        )
    return rent_roll.model_copy(
        update={
            "stated_totals": {**rent_roll.stated_totals, column: value},
            "stated_total_citations": {
                **rent_roll.stated_total_citations,
                column: cited.model_copy(update={"provenance": "manual_override"}),
            },
        }
    )


def apply_data_overrides(
    rent_roll: RentRoll, t12: T12Statement | None, overrides: Sequence[Override]
) -> tuple[RentRoll, T12Statement | None]:
    """Apply the data overrides in order. Inputs are left untouched; applying twice is a no-op."""
    for override in overrides:
        if override.action in ALLOWED_STRATEGIES:
            require_strategy(override)
        if override.action == "remap_line":
            t12 = _remap(t12, *parse_remap(override.target))
        elif override.action == "edit_value":
            rent_roll = _edit(rent_roll, *parse_edit(override.target), money_value(override))
        elif override.action == "resolve_footing":
            rent_roll = _resolve_footing(rent_roll, override.target, money_value(override))
    return rent_roll, t12


def apply_review(
    pkg: DealPackage, overrides: Sequence[Override], *, cfg: AppConfig, deal_dir: Path
) -> DealPackage:
    """Re-run the gate from FOOTING on ``pkg`` with the reviewer's overrides applied.

    ``pkg`` must be the package as extracted (no overrides); pass the *whole* override list each
    time. This never calls the LLM: extraction is not repeated, and only text-layer and Excel
    documents get an independent re-read.
    """
    rent_roll, t12 = apply_data_overrides(pkg.rent_roll, pkg.t12, overrides)
    if rent_roll.as_of_date is None:
        raise OverrideError("the package has no as-of date")
    source = deal_dir / rent_roll.source_file
    reread = make_rereader(source, rent_roll, None) if source.exists() else None
    ctx = CheckContext(
        rent_roll=rent_roll,
        t12=t12,
        proforma=pkg.proforma,
        thresholds=cfg.thresholds,
        as_of=rent_roll.as_of_date,
    )
    result = run_gate(ctx, reread=reread, overrides=status_overrides(overrides))
    return pkg.model_copy(
        update={
            "rent_roll": rent_roll,
            "t12": t12,
            "findings": result.findings,
            "gate_log": result.gate_log,
            "final_state": result.final_state,
            "overrides": list(overrides),
        }
    )
