"""Human review: overrides that change extracted data, then a fresh gate run (SPEC 8.4).

``clear`` and ``override`` only change a finding's status, and the gate applies them itself.
``edit_value`` and ``remap_line`` change the extracted data, so they are applied here, before the
gate, which then runs again from FOOTING. The ``Override`` schema has no payload field, so the
change travels in ``target``:

    remap_line   "<line label>=<BUCKET>"           e.g. "Gas=UTILITIES"
    edit_value   "<unit id>:<field>=<amount>"       e.g. "101:in_place_rent=1250.00"

Both stay human-readable in the saved package, and the note says why.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import get_args

from cre_screen.config import AppConfig
from cre_screen.errors import OverrideError, ParseError
from cre_screen.ingest.parse import parse_money
from cre_screen.ingest.reread import make_rereader
from cre_screen.reconcile.context import CheckContext
from cre_screen.reconcile.gate import run_gate
from cre_screen.reconcile.metrics import normalize_unit_id
from cre_screen.schemas.common import COABucket
from cre_screen.schemas.deal import DealPackage
from cre_screen.schemas.findings import Override
from cre_screen.schemas.rent_roll import RentRoll
from cre_screen.schemas.t12 import T12Statement

EDIT_FIELDS = ("in_place_rent", "market_rent")
BUCKETS: tuple[str, ...] = get_args(COABucket)
STATUS_ACTIONS = ("clear", "override")


def remap_target(label: str, bucket: str) -> str:
    return f"{label}={bucket}"


def edit_target(unit_id: str, field: str, value: Decimal | str) -> str:
    return f"{unit_id}:{field}={value}"


def parse_remap(target: str) -> tuple[str, str]:
    label, sep, bucket = target.rpartition("=")
    if not sep or not label or bucket not in BUCKETS:
        raise OverrideError(f"remap_line target must be '<line label>=<BUCKET>' (got {target!r})")
    return label, bucket


def parse_edit(target: str) -> tuple[str, str, Decimal]:
    head, sep, raw = target.rpartition("=")
    unit_id, colon, field = head.rpartition(":")
    if not (sep and colon and unit_id) or field not in EDIT_FIELDS:
        raise OverrideError(
            f"edit_value target must be '<unit id>:<{'|'.join(EDIT_FIELDS)}>=<amount>' "
            f"(got {target!r})"
        )
    try:
        value = parse_money(raw)
    except ParseError as exc:
        raise OverrideError(f"edit_value amount is not money: {raw!r}") from exc
    if value is None:
        raise OverrideError("edit_value needs an amount")
    try:
        if value != Decimal(raw.strip().replace(",", "").replace("$", "")):
            raise OverrideError("edit_value amounts have at most two decimal places")
    except InvalidOperation:
        pass
    return unit_id, field, value


def status_overrides(overrides: Sequence[Override]) -> list[Override]:
    """The overrides the gate itself applies (they only change a finding's status)."""
    return [o for o in overrides if o.action in STATUS_ACTIONS]


def apply_data_overrides(
    rent_roll: RentRoll, t12: T12Statement | None, overrides: Sequence[Override]
) -> tuple[RentRoll, T12Statement | None]:
    """Apply ``edit_value`` and ``remap_line`` in order. Inputs are left untouched."""
    for override in overrides:
        if override.action == "remap_line":
            label, bucket = parse_remap(override.target)
            if t12 is None or label not in {ln.label for ln in t12.lines}:
                raise OverrideError(f"remap_line: no T-12 line labelled {label!r}")
            lines = [
                ln.model_copy(
                    update={
                        "bucket": bucket,
                        "mapping_source": "manual_override",
                        "mapping_confidence": 1.0,
                        "audit_flags": [*ln.audit_flags, "remapped_by_reviewer"],
                    }
                )
                if ln.label == label
                else ln
                for ln in t12.lines
            ]
            t12 = t12.model_copy(update={"lines": lines})
        elif override.action == "edit_value":
            unit_id, field, value = parse_edit(override.target)
            wanted = normalize_unit_id(unit_id)
            index = next(
                (
                    i
                    for i, u in enumerate(rent_roll.units)
                    if normalize_unit_id(u.unit_id) == wanted
                ),
                None,
            )
            if index is None:
                raise OverrideError(f"edit_value: no unit {unit_id!r} in the rent roll")
            unit = rent_roll.units[index]
            origin = unit.citations.get(field) or unit.citations["unit_id"].model_copy(
                update={"raw_text": ""}
            )
            edited = unit.model_copy(
                update={
                    field: value,
                    "citations": {
                        **unit.citations,
                        field: origin.model_copy(update={"provenance": "manual_override"}),
                    },
                    "audit_flags": [*unit.audit_flags, f"edited_{field}"],
                }
            )
            units = list(rent_roll.units)
            units[index] = edited
            rent_roll = rent_roll.model_copy(update={"units": units})
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
