from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from cre_screen.errors import OverrideError
from cre_screen.reconcile.checks import REGISTRY
from cre_screen.reconcile.gate import run_gate
from cre_screen.reconcile.reread import RereadResult
from cre_screen.schemas.findings import Override
from tests.builders import context, line, roll, subtotal, t12, tweak, unit

D = Decimal
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


class FakeReread:
    def __init__(self, result: RereadResult) -> None:
        self.result = result
        self.blocks: list[list[str]] = []

    def __call__(self, units):
        self.blocks.append([u.unit_id for u in units])
        return self.result


def states(result) -> list[tuple[str, str]]:
    return [(t.from_state, t.to_state) for t in result.gate_log]


def foots():
    return roll([unit("1", "1000.00"), unit("2", "1500.00")], in_place_total="2500.00")


def fails():
    return roll([unit("1", "1000.00"), unit("2", "1500.00")], in_place_total="5200.00")


def override(check_id="RR_FOOTING", action="override", target="*", note="owner confirmed total"):
    return Override(check_id=check_id, action=action, target=target, note=note, at=NOW)


# ---------- transition sequences ----------


def test_clean_deal_goes_footing_checks_ready() -> None:
    result = run_gate(context(foots()), now=lambda: NOW)
    assert states(result) == [("FOOTING", "CHECKS"), ("CHECKS", "READY")]
    assert result.final_state == "READY"
    assert all(t.at == NOW and t.reason for t in result.gate_log)
    assert result.findings == []


def test_unverifiable_footing_still_proceeds_to_checks() -> None:
    result = run_gate(context(roll([unit("1")])))
    assert states(result) == [("FOOTING", "CHECKS"), ("CHECKS", "READY")]
    assert "not verifiable" in result.gate_log[0].reason
    assert [f.severity for f in result.findings] == ["info"]


def test_footing_failure_with_agreeing_reread_is_a_document_error() -> None:
    reread = FakeReread(RereadResult(agrees=True))
    result = run_gate(context(fails()), reread=reread)
    assert states(result) == [
        ("FOOTING", "REEXTRACT"),
        ("REEXTRACT", "CHECKS"),
        ("CHECKS", "REVIEW"),
    ]
    (finding,) = [f for f in result.findings if f.check_id == "RR_FOOTING"]
    assert finding.details["cause"] == "document"
    assert result.final_state == "REVIEW"
    assert "document itself does not foot" in result.gate_log[1].reason
    assert reread.blocks == [["1", "2"]], "no subtotals: the whole table is re-read"


def test_footing_failure_with_disagreeing_reread_is_an_extraction_error() -> None:
    reread = FakeReread(RereadResult(agrees=False, disagreeing_unit_ids=["2"]))
    result = run_gate(context(fails()), reread=reread)
    (finding,) = [f for f in result.findings if f.check_id == "RR_FOOTING"]
    assert finding.details["cause"] == "extraction"
    assert finding.unit_ids == ["2"]
    assert states(result)[:2] == [("FOOTING", "REEXTRACT"), ("REEXTRACT", "CHECKS")]
    assert "extraction" in result.gate_log[1].reason


def test_reextract_never_loops_back_to_footing() -> None:
    result = run_gate(context(fails()), reread=FakeReread(RereadResult(agrees=True)))
    assert [t.to_state for t in result.gate_log].count("FOOTING") == 0
    assert [t.from_state for t in result.gate_log].count("REEXTRACT") == 1


def test_the_smallest_failing_subtotal_is_the_block_that_gets_re_read() -> None:
    units = [unit(str(i), "100.00") for i in range(1, 7)]
    rr = roll(
        units,
        in_place_total="999.00",
        subtotals=[
            subtotal("Page 1", "300.00", ["1", "2", "3"]),  # foots
            subtotal("Page 2", "999.00", ["4", "5", "6"]),  # fails: small block
            subtotal("Wide", "999.00", ["1", "2", "3", "4", "5", "6"]),  # fails: larger block
        ],
    )
    reread = FakeReread(RereadResult(agrees=True))
    run_gate(context(rr), reread=reread)
    assert reread.blocks == [["4", "5", "6"]]


def test_no_reread_available_leaves_the_cause_unverified() -> None:
    result = run_gate(context(fails()), reread=None)
    assert states(result)[0] == ("FOOTING", "CHECKS")
    (finding,) = [f for f in result.findings if f.check_id == "RR_FOOTING"]
    assert finding.details["cause"] == "unverified"
    assert result.final_state == "REVIEW"


def test_zero_reextract_attempts_skips_the_reread() -> None:
    reread = FakeReread(RereadResult(agrees=True))
    never = tweak("rr_footing", max_reextract_attempts=0)
    result = run_gate(context(fails(), thresholds=never), reread=reread)
    assert reread.blocks == [] and states(result)[0] == ("FOOTING", "CHECKS")


def test_a_market_rent_footing_failure_alone_does_not_trigger_a_reread() -> None:
    rr = foots().model_copy(
        update={
            "stated_totals": {"in_place_rent": D("2500.00"), "market_rent": D("500.00")},
            "stated_total_citations": {
                "in_place_rent": foots().stated_total_citations["in_place_rent"],
                "market_rent": foots().stated_total_citations["in_place_rent"],
            },
        }
    )
    reread = FakeReread(RereadResult(agrees=True))
    result = run_gate(context(rr), reread=reread)
    assert reread.blocks == [] and result.final_state == "READY"
    assert [f.severity for f in result.findings] == ["warn"]


# ---------- review and overrides ----------


def test_warnings_alone_do_not_block() -> None:
    rr = roll([unit("1", "0.00")], in_place_total="0.00")
    result = run_gate(context(rr))
    assert result.final_state == "READY"
    assert [f.check_id for f in result.findings] == ["RR_ZERO_RENT_OCC"]


def test_an_open_block_sends_the_deal_to_review() -> None:
    result = run_gate(context(fails()))
    assert result.gate_log[-1].to_state == "REVIEW"
    assert "1 open block" in result.gate_log[-1].reason


def duplicated():
    return roll([unit("101", row=1), unit("101", row=2)])


def test_an_override_with_a_note_clears_a_non_math_block_to_ready() -> None:
    result = run_gate(context(duplicated()), overrides=[override("RR_DUP_UNIT")])
    (finding,) = [f for f in result.findings if f.check_id == "RR_DUP_UNIT"]
    assert finding.status == "overridden" and finding.reviewer_note == "owner confirmed total"
    assert result.final_state == "READY"
    assert states(result)[-1] == ("CHECKS", "READY")


def test_clear_action_marks_a_non_math_finding_cleared() -> None:
    result = run_gate(context(duplicated()), overrides=[override("RR_DUP_UNIT", action="clear")])
    (dup,) = [f for f in result.findings if f.check_id == "RR_DUP_UNIT"]
    assert dup.status == "cleared" and result.final_state == "READY"


@pytest.mark.parametrize("action", ["override", "clear"])
def test_a_bare_override_of_a_footing_block_is_rejected_by_the_engine(action: str) -> None:
    with pytest.raises(OverrideError, match=r"math failure.*resolution_strategy"):
        run_gate(context(fails()), overrides=[override(action=action)])


def test_a_footing_override_with_a_strategy_still_cannot_pass_as_a_status_change() -> None:
    sneaky = override().model_copy(update={"resolution_strategy": "use_calculated_sum"})
    with pytest.raises(OverrideError, match="math failure"):
        run_gate(context(fails()), overrides=[sneaky])


def test_only_rent_roll_footing_is_strict_math() -> None:
    from cre_screen.reconcile.gate import STRICT_MATH_CHECKS

    assert STRICT_MATH_CHECKS == ("RR_FOOTING",)


def test_a_t12_footing_block_can_be_accepted_with_a_note() -> None:
    broken = t12([line("Rent", "GROSS_POTENTIAL_RENT", "100.00", stated="1500.00")])
    open_ = run_gate(context(foots(), t12=broken))
    assert [f.check_id for f in open_.findings if f.severity == "block"] == ["T12_FOOTING"]
    accepted = run_gate(context(foots(), t12=broken), overrides=[override("T12_FOOTING")])
    (finding,) = [f for f in accepted.findings if f.check_id == "T12_FOOTING"]
    assert finding.status == "overridden" and accepted.final_state == "READY"


def test_a_footing_override_with_nothing_to_match_is_still_ignored() -> None:
    result = run_gate(context(foots()), overrides=[override()])
    assert result.final_state == "READY" and result.findings == []


def test_override_for_a_different_check_does_not_clear_the_block() -> None:
    result = run_gate(context(fails()), overrides=[override(check_id="RR_DUP_UNIT")])
    assert result.final_state == "REVIEW"


def test_override_targets_a_unit_or_a_line_label() -> None:
    dup = roll([unit("101", row=1), unit("101", row=2)])
    miss = run_gate(context(dup), overrides=[override("RR_DUP_UNIT", target="999")])
    hit = run_gate(context(dup), overrides=[override("RR_DUP_UNIT", target="101")])
    assert miss.final_state == "REVIEW" and hit.final_state == "READY"


def test_note_shorter_than_the_configured_minimum_is_rejected() -> None:
    strict = tweak("review", override_note_min_chars=40)
    with pytest.raises(OverrideError, match="at least 40"):
        run_gate(context(duplicated(), thresholds=strict), overrides=[override("RR_DUP_UNIT")])


@pytest.mark.parametrize("action", ["edit_value", "remap_line", "resolve_footing"])
def test_data_changing_actions_belong_to_the_review_ui(action: str) -> None:
    with pytest.raises(OverrideError, match="review UI"):
        run_gate(context(fails()), overrides=[override(action=action)])


# ---------- shape ----------


def test_footing_findings_come_first_then_registry_order() -> None:
    rr = roll(
        [unit("1", "0.00"), unit("1", "0.00")],  # duplicate and zero rent
        in_place_total="123.00",
    )
    result = run_gate(context(rr))
    order = list(REGISTRY)
    positions = [order.index(f.check_id) for f in result.findings]
    assert positions == sorted(positions) and result.findings[0].check_id == "RR_FOOTING"


def test_the_gate_is_deterministic() -> None:
    first = run_gate(context(fails()), now=lambda: NOW)
    second = run_gate(context(fails()), now=lambda: NOW)
    assert first.findings == second.findings and first.gate_log == second.gate_log
