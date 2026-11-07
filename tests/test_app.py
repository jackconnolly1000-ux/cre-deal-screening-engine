"""The review UI under streamlit.testing.v1.AppTest (SPEC section 12, M7).

The app runs in replay mode against the committed dev deals. Nothing here calls the API: deal 003
needs no LLM at all, and deal 005's only model call is a recorded cache entry.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from cre_screen.mapping.llm_client import AnthropicLLMClient

APP = Path(__file__).resolve().parents[1] / "src" / "cre_screen" / "app" / "streamlit_app.py"
LONG_NOTE = "seller confirmed the stated total"


@pytest.fixture(autouse=True)
def replay_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRE_LLM_CACHE_MODE", "replay")
    monkeypatch.delenv("CRE_SHOW_HELDOUT", raising=False)
    st.cache_data.clear()  # the app caches the pipeline; every test starts cold


def new_app() -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    assert not at.exception, at.exception
    return at


def screened(deal: str) -> AppTest:
    at = new_app()
    at.selectbox(key="deal").select(deal)
    at.button(key="run").click()
    at.run()
    assert not at.exception, at.exception
    return at


def metrics(at: AppTest) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}


def export_button(at: AppTest):
    (button,) = at.get("download_button")
    return button


# ---------- loading ----------


def test_the_app_loads_with_the_dev_deals_and_no_held_out_ones() -> None:
    at = new_app()
    options = at.selectbox(key="deal").options
    assert options[:14] == [f"deal_{n:03d}" for n in range(1, 15)]
    assert not any(o in options for o in (f"deal_{n:03d}" for n in range(15, 21)))
    assert at.title[0].value == "cre-screen"
    assert any("Press Run" in i.value for i in at.info)


def test_the_cache_mode_is_shown_read_only_and_is_replay() -> None:
    at = new_app()
    box = at.text_input(key="cache_mode_view")
    assert box.value == "replay" and box.disabled is True


def test_nothing_is_screened_until_run_is_pressed() -> None:
    at = new_app()
    at.selectbox(key="deal").select("deal_003")
    at.run()
    assert not at.metric and not at.get("download_button")
    assert any("Press Run to screen deal_003" in i.value for i in at.info)


# ---------- deal 003 in replay mode ----------


def test_deal_003_loads_in_replay_mode_and_shows_its_gate_result() -> None:
    at = screened("deal_003")
    shown = metrics(at)
    assert shown["Gate state"] == "REVIEW"
    assert shown["Open block findings"] == "1" and shown["Open warn findings"] == "2"
    assert shown["Cost of this run"] == "$0"
    assert shown["Lifetime LLM spend"].startswith("$")
    assert [t.label for t in at.tabs] == ["Findings", "Audit view", "T-12 mapping", "Summary"]


def test_the_findings_table_renders_sorted_block_warn_info() -> None:
    at = screened("deal_003")
    table = at.dataframe[0].value
    assert list(table["Severity"]) == ["block", "warn", "warn", "info"]
    assert list(table["Check"]) == [
        "RR_FOOTING",
        "RR_ZERO_RENT_OCC",
        "T12_SPIKE",
        "RR_ZERO_RENT_OCC",
    ]
    assert list(table.columns)[:4] == ["Check", "Severity", "Status", "Message"]


def test_the_export_button_is_disabled_while_blocks_are_open_and_the_count_is_shown() -> None:
    at = screened("deal_003")
    assert export_button(at).proto.disabled is True
    assert any("1 open block finding(s)" in m.value for m in at.markdown)


def test_a_valid_override_note_enables_the_export() -> None:
    at = screened("deal_003")
    at.text_input(key="review_note").input(LONG_NOTE)
    at.button(key="review_apply").click()
    at.run()
    assert not at.exception, at.exception
    assert metrics(at)["Gate state"] == "READY" and metrics(at)["Open block findings"] == "0"
    assert export_button(at).proto.disabled is False
    assert any("No open block findings" in m.value for m in at.markdown)
    assert at.session_state["overrides"][0]["note"] == LONG_NOTE
    assert at.session_state["overrides"][0]["check_id"] == "RR_FOOTING"


def test_a_short_note_is_rejected_and_the_export_stays_disabled() -> None:
    at = screened("deal_003")
    at.text_input(key="review_note").input("too short")
    at.button(key="review_apply").click()
    at.run()
    assert [e.value for e in at.error] == ["A reviewer note needs at least 10 characters."]
    assert at.session_state["overrides"] == []
    assert metrics(at)["Gate state"] == "REVIEW" and export_button(at).proto.disabled is True


def test_an_empty_note_is_rejected_too() -> None:
    at = screened("deal_003")
    at.button(key="review_apply").click()
    at.run()
    assert len(at.error) == 1 and at.session_state["overrides"] == []


def test_a_reviewer_can_clear_a_warning_instead_of_overriding_a_block() -> None:
    at = screened("deal_003")
    at.selectbox(key="review_finding").select_index(1)  # the RR_ZERO_RENT_OCC warning
    at.run()
    at.radio(key="review_action").set_value("clear")
    at.text_input(key="review_note").input("unit 204 is a courtesy unit")
    at.button(key="review_apply").click()
    at.run()
    assert metrics(at)["Open warn findings"] == "1" and metrics(at)["Gate state"] == "REVIEW"
    assert at.session_state["overrides"][0]["action"] == "clear"
    assert at.session_state["overrides"][0]["target"] == "204"


def test_undoing_the_last_override_closes_the_export_again() -> None:
    at = screened("deal_003")
    at.text_input(key="review_note").input(LONG_NOTE)
    at.button(key="review_apply").click()
    at.run()
    assert export_button(at).proto.disabled is False
    at.button(key="review_undo").click()
    at.run()
    assert at.session_state["overrides"] == [] and export_button(at).proto.disabled is True


def test_overrides_are_listed_with_their_notes() -> None:
    at = screened("deal_003")
    at.text_input(key="review_note").input(LONG_NOTE)
    at.button(key="review_apply").click()
    at.run()
    shown = [df.value for df in at.dataframe if "Note" in df.value.columns]
    assert shown and list(shown[0]["Note"]) == [LONG_NOTE]


# ---------- other deals and the audit and summary views ----------


def test_a_clean_deal_exports_straight_away() -> None:
    at = screened("deal_001")
    assert metrics(at)["Gate state"] == "READY"
    assert export_button(at).proto.disabled is False
    assert any("No findings" in s.value for s in at.success)


def test_the_summary_tab_shows_occupancy_and_the_noi_walk() -> None:
    shown = metrics(screened("deal_003"))
    for label in ("Physical occupancy", "Economic occupancy (T-12)", "Monthly loss to lease",
                  "T-12 actual", "T-3 annualized", "Broker pro forma", "Underwritten"):  # fmt: skip
        assert label in shown
    assert shown["Physical occupancy"].endswith("%") and shown["Underwritten"].startswith("$")


def test_the_audit_view_shows_a_page_image_for_a_text_pdf() -> None:
    at = screened("deal_003")
    assert at.session_state["focus"] is None, "no selection yet: the audit view falls back"
    images = at.get("image")
    assert len(images) == 1


def test_a_workbook_deal_has_no_page_image_but_lists_its_rows() -> None:
    at = screened("deal_009")  # an Excel rent roll
    assert not at.get("image")
    assert any("no page image" in i.value for i in at.info)


def test_a_scanned_deal_runs_from_the_recorded_vision_cache() -> None:
    at = screened("deal_010")
    assert not at.exception and metrics(at)["Cost of this run"] == "$0"
    assert [e.value for e in at.error] == []


def test_a_bad_as_of_date_is_reported_not_crashed() -> None:
    at = new_app()
    at.selectbox(key="deal").select("deal_001")
    at.text_input(key="as_of").input("not-a-date")
    at.button(key="run").click()
    at.run()
    assert not at.exception and at.error and "date" in at.error[0].value.lower()


def test_a_deal_folder_without_a_rent_roll_is_reported_not_crashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cre_screen.app import logic

    empty = tmp_path / "broken_deal"
    empty.mkdir()
    monkeypatch.setattr(
        logic, "list_deals", lambda *_a, **_k: [logic.DealRef("broken_deal", empty, "real")]
    )
    at = new_app()
    at.selectbox(key="deal").select("broken_deal")
    at.button(key="run").click()
    at.run()
    assert not at.exception, at.exception
    assert any("needs exactly one rent_roll" in e.value for e in at.error)
    assert not at.metric


# ---------- no click but Run reaches the LLM ----------


def test_reviewing_never_calls_the_llm_even_on_a_deal_that_used_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    real = AnthropicLLMClient.structured

    def counting(self, **kwargs):
        calls.append(kwargs["task"])
        return real(self, **kwargs)

    monkeypatch.setattr(AnthropicLLMClient, "structured", counting)
    at = screened("deal_005")  # one recorded one-time-income label goes through the model path
    after_run = len(calls)
    assert after_run >= 1 and not at.exception
    at.selectbox(key="review_finding").select_index(0)
    at.text_input(key="review_note").input("reviewed against the broker package")
    at.button(key="review_apply").click()
    at.run()
    at.button(key="review_undo").click()
    at.run()
    assert len(calls) == after_run, "reviewing re-runs only the pure gate"
    assert not at.exception


def test_running_the_same_deal_twice_uses_the_cached_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    real = AnthropicLLMClient.structured

    def counting(self, **kwargs):
        calls.append(kwargs["task"])
        return real(self, **kwargs)

    monkeypatch.setattr(AnthropicLLMClient, "structured", counting)
    at = screened("deal_005")
    first = len(calls)
    at.button(key="run").click()
    at.run()
    assert len(calls) == first and not at.exception


# ---------- a footing failure must be resolved, not waved through ----------


def pick_strategy(at: AppTest, strategy: str) -> AppTest:
    at.radio(key="footing_strategy").set_value(strategy)
    at.run()
    assert not at.exception, at.exception
    return at


def test_a_footing_failure_asks_which_number_governs_instead_of_offering_a_bare_override() -> None:
    at = screened("deal_003")
    shown = metrics(at)
    assert shown["Stated total (printed)"] == "$73,703.00"
    assert shown["Calculated sum (unit rows)"] == "$37,703.00"
    assert shown["Difference"] == "$-36,000.00"
    from cre_screen.app import logic

    assert at.radio(key="footing_strategy").options == list(logic.STRATEGY_LABELS.values())
    assert not [r for r in at.radio if r.key == "review_action"], "no bare override for footing"


def test_use_calculated_sum_resolves_the_block_and_records_the_corrected_value() -> None:
    at = screened("deal_003")
    at.text_input(key="review_note").input("digits swapped in the printed total")
    at.button(key="review_apply").click()
    at.run()
    assert not at.exception, at.exception
    (o,) = at.session_state["overrides"]
    assert (o["action"], o["resolution_strategy"], o["corrected_value"]) == (
        "resolve_footing",
        "use_calculated_sum",
        "37703.00",
    )
    assert metrics(at)["Gate state"] == "READY" and export_button(at).proto.disabled is False


def test_use_stated_total_is_refused_until_the_rows_actually_foot() -> None:
    at = pick_strategy(screened("deal_003"), "use_stated_total")
    at.selectbox(key="footing_unit").select("104")
    at.text_input(key="footing_amount").input("1500.00")
    at.text_input(key="review_note").input("keying the missing rent")
    at.button(key="review_apply").click()
    at.run()
    assert any("still miss the stated total" in e.value for e in at.error)
    assert at.session_state["overrides"] == []
    assert metrics(at)["Gate state"] == "REVIEW"


def test_use_stated_total_with_the_exact_amount_clears_the_block_through_the_data() -> None:
    at = pick_strategy(screened("deal_003"), "use_stated_total")
    at.selectbox(key="footing_unit").select("104")
    at.text_input(key="footing_amount").input("36000.00")
    at.text_input(key="review_note").input("keying the missing rent")
    at.button(key="review_apply").click()
    at.run()
    assert not at.exception, at.exception
    (o,) = at.session_state["overrides"]
    assert (o["action"], o["target"], o["corrected_value"]) == (
        "edit_value",
        "104:in_place_rent",
        "36000.00",
    )
    assert metrics(at)["Gate state"] == "READY" and export_button(at).proto.disabled is False


def test_a_footing_resolution_needs_a_note_too() -> None:
    at = screened("deal_003")
    at.text_input(key="review_note").input("short")
    at.button(key="review_apply").click()
    at.run()
    assert [e.value for e in at.error] == ["A reviewer note needs at least 10 characters."]
    assert at.session_state["overrides"] == []
