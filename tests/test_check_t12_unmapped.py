from __future__ import annotations

from cre_screen.reconcile.checks.t12_unmapped import check
from tests.builders import context, line, roll, t12


def run(*lines):
    return check(context(roll([]), t12=t12(list(lines))))


def test_fully_mapped_t12_passes() -> None:
    assert run(line("Taxes", "REAL_ESTATE_TAXES"), line("Debt Service", "NON_OPERATING")) == []


def test_an_unmapped_line_blocks() -> None:
    (f,) = run(line("Taxes", "REAL_ESTATE_TAXES"), line("Mystery", "UNMAPPED"))
    assert f.check_id == "T12_UNMAPPED" and f.severity == "block"
    assert f.line_labels == ["Mystery"] and f.details["count"] == "1"


def test_exactly_one_unmapped_line_is_enough() -> None:
    assert len(run(line("Mystery", "UNMAPPED"))) == 1


def test_all_unmapped_lines_are_listed_in_one_finding() -> None:
    (f,) = run(line("A", "UNMAPPED"), line("B", "UNMAPPED"), line("C", "MARKETING"))
    assert f.line_labels == ["A", "B"] and f.details["count"] == "2"


def test_empty_t12_gives_no_findings() -> None:
    assert run() == []


def test_no_t12_gives_no_findings() -> None:
    assert check(context(roll([]), t12=None)) == []
