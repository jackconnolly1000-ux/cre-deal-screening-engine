from __future__ import annotations

from decimal import Decimal

import pytest

from cre_screen.config import load_config
from cre_screen.mapping.concessions import is_grounded, parse_concessions, plain_number
from cre_screen.schemas.citation import Citation
from cre_screen.schemas.mapping import ConcessionParse
from tests.fakes import FakeLLMClient

D = Decimal
CFG = load_config().thresholds.rr_concession
CITE = Citation(
    source_file="rent_roll.pdf", page=1, row_index=3, raw_text="note", provenance="text_layer"
)


def parse(note: str, rent: str | None = "1200.00", fake: FakeLLMClient | None = None):
    return parse_concessions(
        note,
        in_place_rent=None if rent is None else D(rent),
        citation=CITE,
        cfg=CFG,
        llm=fake or FakeLLMClient(),
    )


# ---------- regex fast path ----------


def test_free_months_on_a_stated_term_makes_no_llm_call() -> None:
    fake = FakeLLMClient()
    (c,) = parse("1 mo free on 12 mo lease", fake=fake)
    assert fake.calls == []
    assert c.kind == "free_months"
    assert c.months_free == D("1")
    assert c.lease_term_months == 12
    assert c.monthly_equivalent == D("100.00")
    assert c.grounded is True


def test_free_months_without_a_term_uses_the_default() -> None:
    (c,) = parse("2 months free", rent="1500.00")
    assert c.lease_term_months == CFG.default_lease_term_months == 12
    assert c.monthly_equivalent == D("250.00")


def test_fractional_free_months() -> None:
    (c,) = parse("1.5 mo free on 18 month lease", rent="1200.00")
    assert c.months_free == D("1.5")
    assert c.lease_term_months == 18
    assert c.monthly_equivalent == D("100.00")


@pytest.mark.parametrize(
    ("note", "amount"),
    [("$100 off/mo", "100.00"), ("150 off per mo", "150.00"), ("$1,200.50 discount mo", "1200.50")],
)
def test_monthly_discount_regex(note: str, amount: str) -> None:
    fake = FakeLLMClient()
    (c,) = parse(note, fake=fake)
    assert fake.calls == []
    assert c.kind == "monthly_discount"
    assert c.monthly_discount == D(amount)
    assert c.monthly_equivalent == D(amount)


def test_a_note_with_both_kinds_gives_two_concessions() -> None:
    result = parse("1 mo free on 12 mo lease and $50 off/mo")
    assert [c.kind for c in result] == ["free_months", "monthly_discount"]


def test_free_months_equivalent_rounds_half_up_to_cents() -> None:
    (c,) = parse("1 mo free on 12 mo lease", rent="1250.00")
    assert c.monthly_equivalent == D("104.17")  # 104.1666...


def test_free_months_without_rent_has_zero_equivalent() -> None:
    (c,) = parse("1 mo free", rent=None)
    assert c.monthly_equivalent == D("0.00")


@pytest.mark.parametrize("note", ["", "   "])
def test_blank_note_gives_nothing(note: str) -> None:
    fake = FakeLLMClient()
    assert parse(note, fake=fake) == []
    assert fake.calls == []


# ---------- LLM path and grounding ----------


def llm_for(note: str, **fields) -> FakeLLMClient:
    return FakeLLMClient({("concession", frozenset([note])): ConcessionParse(**fields)})


def test_llm_path_with_grounded_numbers() -> None:
    note = "Two weeks rent credit of $300 at move-in on 13 month lease"
    fake = llm_for(note, kind="one_time_credit", one_time_credit=D("300"), lease_term_months=13)
    (c,) = parse(note, fake=fake)
    assert len(fake.calls_for("concession")) == 1
    assert c.grounded is True
    assert c.kind == "one_time_credit"
    assert c.one_time_credit == D("300.00")
    assert c.lease_term_months == 13
    assert c.monthly_equivalent == D("23.08")  # 300 / 13


def test_llm_number_not_in_the_note_is_ungrounded() -> None:
    note = "Special rate for first year"
    fake = llm_for(note, kind="monthly_discount", monthly_discount=D("75"))
    (c,) = parse(note, fake=fake)
    assert c.grounded is False
    assert c.monthly_equivalent == D("0.00")
    assert c.monthly_discount is None  # the ungrounded number never reaches the record
    assert c.raw_text == note


def test_one_ungrounded_number_fails_the_whole_parse() -> None:
    note = "$300 credit"
    fake = llm_for(note, kind="one_time_credit", one_time_credit=D("300"), lease_term_months=12)
    (c,) = parse(note, fake=fake)
    assert c.grounded is False and c.one_time_credit is None


def test_llm_values_out_of_range_are_ungrounded_not_a_crash() -> None:
    note = "30 weeks of comp"
    fake = llm_for(note, kind="free_months", months_free=D("30"))
    (c,) = parse(note, fake=fake)
    assert c.grounded is False


def test_kind_other_with_no_numbers_is_grounded_and_costs_nothing() -> None:
    note = "Employee unit"
    (c,) = parse(note, fake=llm_for(note, kind="other"))
    assert c.grounded is True and c.monthly_equivalent == D("0.00")


@pytest.mark.parametrize(
    ("value", "note", "expected"),
    [
        (D("1"), "1 mo free", True),
        (D("1"), "13 mo free", False),
        (D("13"), "13 mo free", True),
        (D("150"), "$150 off", True),
        (D("1500"), "$1,500 off", True),
        (D("1"), "1.5 mo free", False),
        (D("1.5"), "1.5 mo free", True),
        (D("12"), "term 112 months", False),
        (D("150.00"), "$150 off", True),
        (12, "12 mo lease", True),
    ],
)
def test_grounding_rule(value, note: str, expected: bool) -> None:
    assert is_grounded(value, note) is expected


def test_plain_number_has_no_trailing_zeros() -> None:
    assert plain_number(D("150.00")) == "150"
    assert plain_number(D("1.50")) == "1.5"
    assert plain_number(13) == "13"
