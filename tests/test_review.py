from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from cre_screen.config import load_config
from cre_screen.errors import OverrideError
from cre_screen.mapping.llm_protocol import NullLLMClient
from cre_screen.pipeline import run_deal
from cre_screen.review import (
    BUCKETS,
    apply_data_overrides,
    apply_review,
    edit_target,
    parse_edit,
    parse_remap,
    remap_target,
    status_overrides,
)
from cre_screen.schemas.findings import Override

D = Decimal
CFG = load_config()
NOW = datetime(2026, 11, 6, tzinfo=UTC)
DEAL = Path(__file__).resolve().parents[1] / "data" / "synthetic" / "deal_003"


@pytest.fixture(scope="module")
def pkg(tmp_path_factory: pytest.TempPathFactory):
    return run_deal(
        DEAL, llm=NullLLMClient(), cfg=CFG, out_dir=tmp_path_factory.mktemp("review_out")
    )


def override(
    action: str,
    target: str,
    note: str = "reviewer checked the document",
    value: Decimal | str | None = None,
    strategy: str | None = None,
) -> Override:
    return Override(
        check_id="T12_UNMAPPED",
        action=action,  # type: ignore[arg-type]
        target=target,
        note=note,
        at=NOW,
        corrected_value=None if value is None else Decimal(str(value)),
        resolution_strategy=strategy,  # type: ignore[arg-type]
    )


# ---------- target encodings ----------


def test_remap_target_round_trips_even_with_an_equals_sign_in_the_label() -> None:
    assert parse_remap(remap_target("Gas", "UTILITIES")) == ("Gas", "UTILITIES")
    assert parse_remap(remap_target("A=B fee", "MARKETING")) == ("A=B fee", "MARKETING")


@pytest.mark.parametrize("target", ["Gas", "Gas=NOT_A_BUCKET", "=UTILITIES", ""])
def test_malformed_remap_targets_are_rejected(target: str) -> None:
    with pytest.raises(OverrideError, match="remap_line target"):
        parse_remap(target)


def test_every_coa_bucket_is_a_valid_remap_destination() -> None:
    assert len(BUCKETS) == 20
    for bucket in BUCKETS:
        assert parse_remap(remap_target("x", bucket))[1] == bucket


def test_edit_target_round_trips() -> None:
    assert parse_edit(edit_target("101", "in_place_rent")) == ("101", "in_place_rent")
    assert parse_edit("A-12:market_rent") == ("A-12", "market_rent")


@pytest.mark.parametrize("target", ["101", "101:beds", ":in_place_rent", "101:in_place_rent=5"])
def test_malformed_edit_targets_are_rejected(target: str) -> None:
    with pytest.raises(OverrideError, match="edit_value target"):
        parse_edit(target)


def test_a_data_override_without_a_corrected_value_is_rejected(pkg) -> None:
    with pytest.raises(OverrideError, match="needs a corrected_value"):
        apply_data_overrides(
            pkg.rent_roll, pkg.t12, [override("edit_value", edit_target("102", "in_place_rent"))]
        )


def test_only_clear_and_override_are_status_overrides() -> None:
    mixed = [
        override("clear", "*"),
        override("remap_line", "Gas=UTILITIES"),
        override("override", "*"),
    ]
    assert [o.action for o in status_overrides(mixed)] == ["clear", "override"]


# ---------- applying data overrides ----------


def test_a_remap_moves_the_line_and_records_who_decided(pkg) -> None:
    line = next(ln for ln in pkg.t12.lines if ln.bucket == "UTILITIES")
    _, t12 = apply_data_overrides(
        pkg.rent_roll, pkg.t12, [override("remap_line", remap_target(line.label, "MARKETING"))]
    )
    moved = next(ln for ln in t12.lines if ln.label == line.label)
    assert moved.bucket == "MARKETING" and moved.mapping_source == "manual_override"
    assert moved.mapping_confidence == 1.0 and "remapped_by_reviewer" in moved.audit_flags
    assert moved.monthly == line.monthly and moved.citation == line.citation


def test_applying_overrides_never_mutates_the_original_package(pkg) -> None:
    before = pkg.model_dump_json()
    label = pkg.t12.lines[0].label
    apply_data_overrides(
        pkg.rent_roll,
        pkg.t12,
        [
            override("remap_line", remap_target(label, "MARKETING")),
            override("edit_value", edit_target("102", "in_place_rent"), value="1.00"),
        ],
    )
    assert pkg.model_dump_json() == before


def test_an_edit_sets_the_value_and_marks_the_citation_as_manual(pkg) -> None:
    original = next(u for u in pkg.rent_roll.units if u.unit_id == "102")
    rr, _ = apply_data_overrides(
        pkg.rent_roll,
        pkg.t12,
        [override("edit_value", edit_target("102", "in_place_rent"), value="1761.00")],
    )
    unit = next(u for u in rr.units if u.unit_id == "102")
    assert unit.in_place_rent == D("1761.00") and original.in_place_rent == D("1760.00")
    cite = unit.citations["in_place_rent"]
    assert cite.provenance == "manual_override"
    assert cite.raw_text == original.citations["in_place_rent"].raw_text, "the printed text is kept"
    assert (cite.page, cite.bbox) == (
        original.citations["in_place_rent"].page, original.citations["in_place_rent"].bbox,
    )  # fmt: skip
    assert "edited_in_place_rent" in unit.audit_flags


def test_editing_a_blank_cell_cites_the_unit_row(pkg) -> None:
    vacant = next(u for u in pkg.rent_roll.units if u.in_place_rent is None)
    rr, _ = apply_data_overrides(
        pkg.rent_roll,
        pkg.t12,
        [override("edit_value", edit_target(vacant.unit_id, "in_place_rent"), value="900")],
    )
    unit = next(u for u in rr.units if u.unit_id == vacant.unit_id)
    assert unit.in_place_rent == D("900.00")
    assert unit.citations["in_place_rent"].provenance == "manual_override"
    assert unit.citations["in_place_rent"].row_index == vacant.citations["unit_id"].row_index


def test_overrides_apply_in_order(pkg) -> None:
    label = pkg.t12.lines[0].label
    _, t12 = apply_data_overrides(
        pkg.rent_roll,
        pkg.t12,
        [
            override("remap_line", remap_target(label, "MARKETING")),
            override("remap_line", remap_target(label, "PAYROLL")),
        ],
    )
    assert next(ln for ln in t12.lines if ln.label == label).bucket == "PAYROLL"


def test_unknown_lines_and_units_are_rejected(pkg) -> None:
    with pytest.raises(OverrideError, match="no T-12 line"):
        apply_data_overrides(pkg.rent_roll, pkg.t12, [override("remap_line", "Nope=MARKETING")])
    with pytest.raises(OverrideError, match="no unit"):
        apply_data_overrides(
            pkg.rent_roll,
            pkg.t12,
            [override("edit_value", edit_target("999", "in_place_rent"), value="1")],
        )
    with pytest.raises(OverrideError, match="no T-12 line"):
        apply_data_overrides(pkg.rent_roll, None, [override("remap_line", "Gas=MARKETING")])


# ---------- the gate runs again from FOOTING ----------


def test_review_with_no_overrides_reproduces_the_gate_result(pkg) -> None:
    again = apply_review(pkg, [], cfg=CFG, deal_dir=DEAL)
    assert again.final_state == pkg.final_state == "REVIEW"
    assert [(f.check_id, f.severity) for f in again.findings] == [
        (f.check_id, f.severity) for f in pkg.findings
    ]
    assert again.gate_log[0].from_state == "FOOTING"


def test_a_status_override_cannot_take_a_footing_failure_to_ready(pkg) -> None:
    sign_off = Override(
        check_id="RR_FOOTING",
        action="override",
        target="*",
        note="seller confirmed the total",
        at=NOW,
    )
    with pytest.raises(OverrideError, match="math failure"):
        apply_review(pkg, [sign_off], cfg=CFG, deal_dir=DEAL)


@pytest.mark.parametrize(
    ("action", "strategy", "value"),
    [
        ("resolve_footing", None, "37703.00"),
        ("resolve_footing", "edit_line_item", "37703.00"),
        ("edit_value", None, "900"),
        ("edit_value", "use_calculated_sum", "900"),
    ],
)
def test_footing_data_overrides_need_the_right_resolution_strategy(
    pkg, action: str, strategy: str | None, value: str
) -> None:
    target = "in_place_rent" if action == "resolve_footing" else "104:in_place_rent"
    bad = Override(
        check_id="RR_FOOTING",
        action=action,  # type: ignore[arg-type]
        target=target,
        corrected_value=Decimal(value),
        resolution_strategy=strategy,  # type: ignore[arg-type]
        note="reviewer corrected the figure",
        at=NOW,
    )
    with pytest.raises(OverrideError, match="needs a resolution_strategy"):
        apply_data_overrides(pkg.rent_roll, pkg.t12, [bad])


def test_fixing_the_footing_number_clears_the_block_the_honest_way(pkg) -> None:
    # The stated total is wrong in the document. A reviewer who keys the missing amount into a
    # blank unit so the rows sum to the stated total clears the footing block, with the edit
    # recorded as manual_override.
    stated = pkg.rent_roll.stated_totals["in_place_rent"]
    computed = sum((u.in_place_rent or D(0) for u in pkg.rent_roll.units), D(0))
    vacant = next(u for u in pkg.rent_roll.units if u.in_place_rent is None)
    fix = override(
        "edit_value", edit_target(vacant.unit_id, "in_place_rent"), value=stated - computed
    )
    reviewed = apply_review(pkg, [fix], cfg=CFG, deal_dir=DEAL)
    assert not [
        f for f in reviewed.findings if f.check_id == "RR_FOOTING" and f.severity == "block"
    ]
    assert reviewed.gate_log[0].from_state == "FOOTING"


def test_remapping_an_unmapped_line_clears_the_t12_unmapped_block(pkg) -> None:
    line = pkg.t12.lines[3]
    stuck = pkg.model_copy(
        update={
            "t12": pkg.t12.model_copy(
                update={
                    "lines": [
                        ln.model_copy(update={"bucket": "UNMAPPED"}) if ln is line else ln
                        for ln in pkg.t12.lines
                    ]
                }
            )
        }
    )
    blocked = apply_review(stuck, [], cfg=CFG, deal_dir=DEAL)
    assert "T12_UNMAPPED" in {f.check_id for f in blocked.findings if f.severity == "block"}
    fixed = apply_review(
        stuck,
        [override("remap_line", remap_target(line.label, line.bucket))],
        cfg=CFG,
        deal_dir=DEAL,
    )
    assert "T12_UNMAPPED" not in {f.check_id for f in fixed.findings}


def test_review_needs_an_as_of_date(pkg) -> None:
    blank = pkg.model_copy(
        update={"rent_roll": pkg.rent_roll.model_copy(update={"as_of_date": None})}
    )
    with pytest.raises(OverrideError, match="as-of"):
        apply_review(blank, [], cfg=CFG, deal_dir=DEAL)


def test_review_works_without_the_source_file_on_disk(pkg, tmp_path: Path) -> None:
    result = apply_review(pkg, [], cfg=CFG, deal_dir=tmp_path)  # no rent_roll.pdf here
    assert result.final_state == "REVIEW"
    footing = next(f for f in result.findings if f.check_id == "RR_FOOTING")
    assert footing.details["cause"] == "unverified", "no re-read was possible"


# ---------- resolve_footing: the printed total is wrong ----------


def calc_sum(pkg) -> Decimal:
    return sum((u.in_place_rent or D(0) for u in pkg.rent_roll.units), D(0))


def resolve(pkg, value=None) -> Override:
    return Override(
        check_id="RR_FOOTING",
        action="resolve_footing",
        target="in_place_rent",
        corrected_value=calc_sum(pkg) if value is None else value,
        resolution_strategy="use_calculated_sum",
        note="the printed grand total has two digits swapped",
        at=NOW,
    )


def test_use_calculated_sum_replaces_the_stated_total_and_the_gate_foots(pkg) -> None:
    reviewed = apply_review(pkg, [resolve(pkg)], cfg=CFG, deal_dir=DEAL)
    assert reviewed.rent_roll.stated_totals["in_place_rent"] == calc_sum(pkg) == D("37703.00")
    assert pkg.rent_roll.stated_totals["in_place_rent"] == D("73703.00"), "original untouched"
    assert not [f for f in reviewed.findings if f.check_id == "RR_FOOTING"]
    assert reviewed.open_blocks() == [] and reviewed.final_state == "READY"
    assert all(f.status != "overridden" for f in reviewed.findings), "fixed, not waved through"


def test_the_replaced_total_keeps_the_printed_text_and_is_marked_manual(pkg) -> None:
    rr, _ = apply_data_overrides(pkg.rent_roll, pkg.t12, [resolve(pkg)])
    cite = rr.stated_total_citations["in_place_rent"]
    original = pkg.rent_roll.stated_total_citations["in_place_rent"]
    assert cite.provenance == "manual_override" and cite.raw_text == original.raw_text
    assert (cite.page, cite.bbox) == (original.page, original.bbox)


def test_use_calculated_sum_rejects_a_value_that_is_not_the_sum(pkg) -> None:
    with pytest.raises(OverrideError, match="not the calculated sum"):
        apply_data_overrides(pkg.rent_roll, pkg.t12, [resolve(pkg, D("37703.01"))])


def test_resolve_footing_needs_a_value_and_a_stated_column(pkg) -> None:
    bare = resolve(pkg).model_copy(update={"corrected_value": None})
    with pytest.raises(OverrideError, match="needs a corrected_value"):
        apply_data_overrides(pkg.rent_roll, pkg.t12, [bare])
    other = resolve(pkg).model_copy(update={"target": "beds"})
    with pytest.raises(OverrideError, match="no stated"):
        apply_data_overrides(pkg.rent_roll, pkg.t12, [other])


def test_applying_data_overrides_twice_changes_nothing_more(pkg) -> None:
    vacant = next(u for u in pkg.rent_roll.units if u.in_place_rent is None)
    both = [
        resolve(pkg),
        override("edit_value", edit_target(vacant.unit_id, "in_place_rent"), value="900"),
        override("remap_line", remap_target(pkg.t12.lines[0].label, "MARKETING")),
    ]
    once = apply_data_overrides(pkg.rent_roll, pkg.t12, both)
    twice = apply_data_overrides(*once, both)
    assert twice[0] == once[0] and twice[1] == once[1]
