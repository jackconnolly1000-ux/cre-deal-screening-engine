from __future__ import annotations

from cre_screen.reconcile.checks.rr_dup_unit import check
from tests.builders import cite, context, roll, unit


def run(*units):
    return check(context(roll(list(units))))


def test_unique_units_pass() -> None:
    assert run(unit("101"), unit("102"), unit("103")) == []


def test_a_repeated_unit_is_a_blocking_finding_with_both_citations() -> None:
    (f,) = run(unit("101", row=1), unit("102", row=2), unit("101", row=7))
    assert f.check_id == "RR_DUP_UNIT" and f.severity == "block"
    assert f.unit_ids == ["101"]
    assert [c.row_index for c in f.citations] == [1, 7]
    assert f.details["copies"] == "2"
    assert "printed 2 times" in f.message


def test_exactly_two_copies_is_the_edge_that_fires() -> None:
    assert len(run(unit("101"), unit("101"))) == 1
    assert run(unit("101")) == []


def test_three_copies_are_one_finding() -> None:
    (f,) = run(unit("101"), unit("101"), unit("101"))
    assert f.details["copies"] == "3" and len(f.citations) == 3


def test_each_duplicated_id_gets_its_own_finding() -> None:
    findings = run(unit("101"), unit("102"), unit("101"), unit("102"))
    assert sorted(f.unit_ids[0] for f in findings) == ["101", "102"]


def test_ids_are_normalized_for_case_and_whitespace() -> None:
    assert len(run(unit("a101"), unit(" A101 "))) == 1
    assert len(run(unit("A 101"), unit("A  101"))) == 1


def test_leading_zeros_are_significant() -> None:
    assert run(unit("0101"), unit("101")) == []


def test_empty_rent_roll_gives_no_findings() -> None:
    assert run() == []


def test_citation_helper_is_usable() -> None:
    assert cite().short() == "doc.pdf p.1 row 0"
