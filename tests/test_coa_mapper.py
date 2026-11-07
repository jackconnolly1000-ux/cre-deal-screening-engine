from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cre_screen.config import load_config
from cre_screen.errors import ConfigError
from cre_screen.mapping.coa_mapper import (
    append_learned,
    has_nonrecurring_keyword,
    load_learned,
    map_labels,
    subtotal_kind,
)
from cre_screen.schemas.mapping import COAMapping, COAMappingBatch
from tests.fakes import FakeLLMClient

COA = load_config().coa


def answer(*items: tuple[str, str, bool]) -> COAMappingBatch:
    return COAMappingBatch(
        mappings=[
            COAMapping(line_label=label, bucket=bucket, is_nonrecurring=nr, rationale="test")  # type: ignore[arg-type]
            for label, bucket, nr in items
        ]
    )


class Scripted:
    """Returns the queued batches in order and records every request."""

    def __init__(self, *batches: COAMappingBatch) -> None:
        self.batches = list(batches)
        self.requests: list[str] = []

    def structured(self, *, task: str, response_model: Any, system: str, user_content, model=None):
        assert task == "coa_map"
        self.requests.append(user_content[0]["text"])
        return self.batches.pop(0)


def test_exact_synonyms_match_ignoring_case_and_punctuation_with_no_llm_call() -> None:
    fake = FakeLLMClient()
    labels = ["R/E TAXES", "repairs & maintenance", "Property Management", "G&A"]
    got = map_labels(labels, COA, fake)
    assert fake.calls == []
    assert {k: v.bucket for k, v in got.items()} == {
        "R/E TAXES": "REAL_ESTATE_TAXES",
        "repairs & maintenance": "REPAIRS_MAINTENANCE",
        "Property Management": "MANAGEMENT_FEE",
        "G&A": "GENERAL_ADMIN",
    }
    assert all(v.source == "synonym_table" and v.confidence == 1.0 for v in got.values())


def test_fuzzy_match_at_or_above_92_makes_no_llm_call() -> None:
    fake = FakeLLMClient()
    (m,) = map_labels(["Real Estate Tax"], COA, fake).values()
    assert fake.calls == []
    assert (m.bucket, m.source) == ("REAL_ESTATE_TAXES", "fuzzy")
    assert 0.92 <= m.confidence < 1.0


def test_a_label_that_only_contains_a_synonym_is_not_fuzzy_matched() -> None:
    # token_set_ratio would call this INSURANCE; strict similarity sends it to the LLM
    llm = Scripted(answer(("Insurance Proceeds", "OTHER_INCOME", True)))
    (m,) = map_labels(["Insurance Proceeds"], COA, llm).values()
    assert len(llm.requests) == 1
    assert (m.bucket, m.source, m.is_nonrecurring) == ("OTHER_INCOME", "llm", True)


def test_unmatched_labels_go_to_the_llm_in_one_batch() -> None:
    llm = Scripted(
        answer(("Zeta Fees", "OTHER_INCOME", False), ("Elevator Phone Line", "UTILITIES", False))
    )
    got = map_labels(["Late Fees", "Zeta Fees", "Elevator Phone Line"], COA, llm)
    assert len(llm.requests) == 1
    assert "Late Fees" not in llm.requests[0]
    assert "Zeta Fees" in llm.requests[0] and "UTILITIES:" in llm.requests[0]
    assert got["Late Fees"].source == "synonym_table"
    assert got["Zeta Fees"].source == "llm" and got["Zeta Fees"].confidence == 0.7
    assert got["Elevator Phone Line"].bucket == "UTILITIES"


def test_fake_client_path_also_works() -> None:
    response = answer(("Zeta Fees", "OTHER_INCOME", False))
    fake = FakeLLMClient({("coa_map", frozenset(["Zeta Fees"])): response})
    assert map_labels(["Zeta Fees"], COA, fake)["Zeta Fees"].bucket == "OTHER_INCOME"
    assert len(fake.calls_for("coa_map")) == 1


def test_missing_labels_are_re_asked_once_then_unmapped() -> None:
    llm = Scripted(answer(("Alpha Item", "MARKETING", False)), answer())
    got = map_labels(["Alpha Item", "Beta Item"], COA, llm)
    assert len(llm.requests) == 2
    assert "Alpha Item" in llm.requests[0] and "Beta Item" in llm.requests[0]
    assert "Beta Item" in llm.requests[1]
    assert "Alpha Item" not in llm.requests[1].split("Labels:")[1]
    assert got["Alpha Item"].bucket == "MARKETING"
    assert got["Beta Item"].bucket == "UNMAPPED"


def test_a_label_recovered_on_the_re_ask_is_kept() -> None:
    llm = Scripted(
        answer(("Alpha Item", "MARKETING", False)), answer(("Beta Item", "PAYROLL", False))
    )
    got = map_labels(["Alpha Item", "Beta Item"], COA, llm)
    assert (got["Beta Item"].bucket, got["Beta Item"].source) == ("PAYROLL", "llm")
    assert len(llm.requests) == 2


def test_unknown_and_repeated_llm_labels_are_ignored() -> None:
    llm = Scripted(
        answer(
            ("Made Up Label", "PAYROLL", False),
            ("Alpha Item", "MARKETING", False),
            ("Alpha Item", "PAYROLL", False),
        )
    )
    got = map_labels(["Alpha Item"], COA, llm)
    assert set(got) == {"Alpha Item"}
    assert got["Alpha Item"].bucket == "MARKETING"


def test_repeated_input_labels_are_sent_once() -> None:
    llm = Scripted(answer(("Alpha Item", "MARKETING", False)))
    map_labels(["Alpha Item", "Alpha Item"], COA, llm)
    assert llm.requests[0].count("Alpha Item") == 1


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Insurance Proceeds", True),
        ("LEASE BUYOUT fee", True),
        ("One-Time Credit", True),
        ("Refund of deposits", True),
        ("Late Fees", False),
        ("Insurance", False),
    ],
)
def test_nonrecurring_keywords_are_case_insensitive(label: str, expected: bool) -> None:
    assert has_nonrecurring_keyword(label, COA) is expected


def test_keyword_sets_nonrecurring_even_when_the_llm_says_no() -> None:
    llm = Scripted(answer(("Insurance Proceeds", "OTHER_INCOME", False)))
    (m,) = map_labels(["Insurance Proceeds"], COA, llm).values()
    assert m.is_nonrecurring is True


def test_llm_saying_nonrecurring_is_honored() -> None:
    llm = Scripted(answer(("Misc Adj", "OTHER_INCOME", True)))
    assert map_labels(["Misc Adj"], COA, llm)["Misc Adj"].is_nonrecurring is True


def test_learned_mappings_are_read_first_and_win() -> None:
    fake = FakeLLMClient()
    learned = {"MARKETING": ["Zeta Fees"], "UTILITIES": ["Gas"]}
    got = map_labels(["Zeta Fees", "Gas"], COA, fake, learned=learned)
    assert fake.calls == []
    assert got["Zeta Fees"].bucket == "MARKETING" and got["Zeta Fees"].source == "synonym_table"
    assert got["Gas"].bucket == "UTILITIES"


def test_learned_file_round_trip_and_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "coa_learned.yaml"
    assert load_learned(path) == {}
    append_learned(path, "MARKETING", "Zeta Fees")
    append_learned(path, "MARKETING", "Zeta Fees")
    append_learned(path, "UTILITIES", "Steam")
    assert load_learned(path) == {"MARKETING": ["Zeta Fees"], "UTILITIES": ["Steam"]}
    assert not list(tmp_path.glob("*.tmp"))


def test_learned_file_with_wrong_shape_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "coa_learned.yaml"
    path.write_text("- not a mapping\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_learned(path)


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("Total Income", "total_income"),
        ("effective gross income", "total_income"),
        ("Total Operating Expenses", "total_expenses"),
        ("Net Operating Income", "noi"),
        ("NOI", "noi"),
        ("Total Misc", None),
        ("Late Fees", None),
    ],
)
def test_subtotal_kind(label: str, kind: str | None) -> None:
    assert subtotal_kind(label, COA) == kind


@pytest.mark.parametrize(
    "label",
    [
        "Court / Eviction",
        "court & eviction",
        "Court Costs",
        "Eviction Fees",
        "Eviction Costs",
        "Legal",
        "Legal Fees",
        "Legal & Professional",
        "Professional Fees",
        "Filing Fees",
    ],
)
def test_legal_and_eviction_labels_map_to_general_admin_without_a_model_call(label: str) -> None:
    fake = FakeLLMClient()
    (m,) = map_labels([label], COA, fake).values()
    assert fake.calls == []
    assert (m.bucket, m.source) == ("GENERAL_ADMIN", "synonym_table")
    assert m.is_nonrecurring is False


def test_a_legal_recovery_is_still_flagged_one_time() -> None:
    llm = Scripted(answer(("Legal Recovery", "OTHER_INCOME", False)))
    (m,) = map_labels(["Legal Recovery"], COA, llm).values()
    assert m.is_nonrecurring is True
