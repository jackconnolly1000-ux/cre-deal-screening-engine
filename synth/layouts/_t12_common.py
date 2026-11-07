"""Shared T-12 structure for the sections and Excel layouts: blocks, subtotals, labels."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from random import Random

from cre_screen.config import load_config
from cre_screen.schemas.common import OPEX_BUCKETS, REVENUE_BUCKETS
from cre_screen.schemas.deal import TruthT12Line

ZERO = Decimal("0.00")
DEDUCTION_BUCKETS = ("LOSS_TO_LEASE", "VACANCY_LOSS", "CONCESSIONS", "BAD_DEBT")


@dataclass(frozen=True)
class Subtotal:
    label: str
    monthly: list[Decimal]
    total: Decimal


@dataclass(frozen=True)
class T12Blocks:
    income: list[TruthT12Line]
    expenses: list[TruthT12Line]
    capital: list[TruthT12Line]
    total_income: Subtotal
    total_expenses: Subtotal
    noi: Subtotal


def _sum_months(lines: list[TruthT12Line]) -> list[Decimal]:
    return [sum((ln.monthly[m] for ln in lines), ZERO) for m in range(12)]


def subtotal_labels(aux: Random) -> tuple[str, str, str]:
    """(total income, total expenses, NOI) labels, drawn from the configured synonyms."""
    cfg = load_config().coa.subtotal_labels
    return (
        aux.choice(cfg.total_income[:2]),
        aux.choice(cfg.total_expenses[:2]),
        aux.choice(cfg.noi[:2]),
    )


def build_blocks(lines: list[TruthT12Line], labels: tuple[str, str, str]) -> T12Blocks:
    """Split lines into income / expenses / capital and compute subtotals from the printed months.

    Subtotals come from the printed (post-trap) month values, so a planted line-level footing
    error is the only thing the T-12 footing check can find.
    """
    income = [ln for ln in lines if ln.true_bucket in REVENUE_BUCKETS]
    expenses = [ln for ln in lines if ln.true_bucket in OPEX_BUCKETS]
    capital = [ln for ln in lines if ln.true_bucket not in (*REVENUE_BUCKETS, *OPEX_BUCKETS)]
    income_months = _sum_months(income)
    expense_months = _sum_months(expenses)
    noi_months = [i - e for i, e in zip(income_months, expense_months, strict=True)]
    return T12Blocks(
        income=income,
        expenses=expenses,
        capital=capital,
        total_income=Subtotal(labels[0], income_months, sum(income_months, ZERO)),
        total_expenses=Subtotal(labels[1], expense_months, sum(expense_months, ZERO)),
        noi=Subtotal(labels[2], noi_months, sum(noi_months, ZERO)),
    )
