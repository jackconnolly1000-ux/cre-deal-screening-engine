from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cre_screen.config import load_config
from cre_screen.errors import DateParseError, MoneyParseError, NumberParseError
from cre_screen.ingest.parse import (
    money_was_rounded,
    normalize_status,
    parse_date,
    parse_decimal,
    parse_int,
    parse_money,
)

D = Decimal
NBSP = chr(0xA0)
MINUS = chr(0x2212)
EN_DASH = chr(0x2013)
EM_DASH = chr(0x2014)


def plain(x: Decimal) -> str:
    return f"{x:f}"


def commas(x: Decimal) -> str:
    return f"{x:,.2f}"


def dollar_commas(x: Decimal) -> str:
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def parens_negative(x: Decimal) -> str:
    return f"({abs(x):,.2f})" if x < 0 else f"{x:,.2f}"


def trailing_minus(x: Decimal) -> str:
    return f"{abs(x):,.2f}-" if x < 0 else f"{x:,.2f}"


FORMATTERS = [plain, commas, dollar_commas, parens_negative, trailing_minus]
amounts = st.decimals(min_value=D("-10000000"), max_value=D("10000000"), places=2)


@pytest.mark.parametrize("fmt", FORMATTERS, ids=lambda f: f.__name__)
@given(x=amounts)
def test_parse_money_round_trips_every_formatter(fmt, x: Decimal) -> None:
    assert parse_money(fmt(x)) == x


@given(x=amounts)
def test_parse_money_result_is_always_two_places(x: Decimal) -> None:
    assert parse_money(commas(x)).as_tuple().exponent == -2  # type: ignore[union-attr]


@pytest.mark.parametrize("text", ["-", EN_DASH, EM_DASH, " - "])
def test_accounting_dash_is_zero(text: str) -> None:
    assert parse_money(text) == D("0.00")


@pytest.mark.parametrize("text", ["", "   ", "N/A", "n/a", NBSP, "$"])
def test_blank_and_na_are_none(text: str) -> None:
    assert parse_money(text) is None


def test_unicode_minus_and_nbsp() -> None:
    assert parse_money(f"{MINUS}123.45") == D("-123.45")
    assert parse_money(f"{NBSP}$1,250.00{NBSP}") == D("1250.00")


def test_more_than_two_decimals_rounds_half_up_to_cents() -> None:
    assert parse_money("10.005") == D("10.01")
    assert parse_money("10.004") == D("10.00")
    assert money_was_rounded("10.005") is True
    assert money_was_rounded("10.00") is False
    assert money_was_rounded("10.000") is False


@pytest.mark.parametrize(
    "text", ["abc", "12.3.4", "(12.00", "12.00)", "--5", "(-5.00)", "1 2", "5%"]
)
def test_garbage_money_raises(text: str) -> None:
    with pytest.raises(MoneyParseError):
        parse_money(text)


# ---------- dates ----------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10/31/2026", date(2026, 10, 31)),
        ("10/31/26", date(2026, 10, 31)),
        ("2026-10-31", date(2026, 10, 31)),
        ("31-Oct-2026", date(2026, 10, 31)),
        ("1/5/2026", date(2026, 1, 5)),
    ],
)
def test_full_date_formats(text: str, expected: date) -> None:
    assert parse_date(text) == expected


@pytest.mark.parametrize(
    ("text", "first", "last"),
    [
        ("Oct-26", date(2026, 10, 1), date(2026, 10, 31)),
        ("Feb 2024", date(2024, 2, 1), date(2024, 2, 29)),
        ("12/2025", date(2025, 12, 1), date(2025, 12, 31)),
        ("Feb 2025", date(2025, 2, 1), date(2025, 2, 28)),
    ],
)
def test_month_only_dates_give_first_or_end_of_month(text: str, first: date, last: date) -> None:
    assert parse_date(text) == first
    assert parse_date(text, end_of_month=True) == last


def test_full_date_ignores_end_of_month_flag() -> None:
    assert parse_date("10/05/2026", end_of_month=True) == date(2026, 10, 5)


def test_excel_datetime_and_date_pass_through() -> None:
    assert parse_date(datetime(2026, 10, 31, 0, 0)) == date(2026, 10, 31)
    assert parse_date(date(2026, 10, 31)) == date(2026, 10, 31)


def test_blank_date_is_none() -> None:
    assert parse_date("") is None
    assert parse_date("  ") is None


@pytest.mark.parametrize("text", ["tomorrow", "13/45/2026", "2026/10/31", "Octember 2026"])
def test_garbage_date_raises(text: str) -> None:
    with pytest.raises(DateParseError):
        parse_date(text)


# ---------- ints and decimals ----------


def test_parse_int_and_decimal() -> None:
    assert parse_int("1,200") == 1200
    assert parse_int("3.0") == 3
    assert parse_int("") is None
    assert parse_decimal("1.5") == D("1.5")
    assert parse_decimal("N/A") is None


@pytest.mark.parametrize("text", ["abc", "3.5x"])
def test_garbage_numbers_raise(text: str) -> None:
    with pytest.raises(NumberParseError):
        parse_decimal(text)
    with pytest.raises(NumberParseError):
        parse_int(text)


def test_parse_int_rejects_fractions() -> None:
    with pytest.raises(NumberParseError):
        parse_int("2.5")


# ---------- status ----------


@pytest.fixture(scope="module")
def rr_cfg():
    return load_config().rr_headers


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Occupied", "occupied"),
        ("CURRENT", "occupied"),
        ("Vacant-Unrented", "vacant"),
        ("NTV", "notice"),
        ("Model", "model"),
        ("Down", "down"),
        ("Employee", "employee"),
        ("mystery", "unknown"),
    ],
)
def test_status_text_is_matched_case_insensitively(rr_cfg, text: str, expected: str) -> None:
    assert normalize_status(text, None, None, rr_cfg) == expected


@pytest.mark.parametrize(
    ("tenant", "rent", "expected"),
    [
        ("VACANT", None, "vacant"),
        ("*vacant*", D("0.00"), "vacant"),
        ("", None, "vacant"),
        ("", D("900.00"), "occupied"),
        ("Jane Doe", D("900.00"), "occupied"),
        ("Jane Doe", None, "occupied"),
    ],
)
def test_without_status_text_tenant_and_rent_decide(
    rr_cfg, tenant: str, rent: Decimal | None, expected: str
) -> None:
    assert normalize_status(None, tenant, rent, rr_cfg) == expected
