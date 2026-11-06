"""Number and date formatting variants. Formats vary per deal to exercise the parsers."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

MONEY_STYLES = ("dollar_commas", "plain", "commas")
DATE_STYLES = ("us_slash", "iso", "day_mon")
NEG_STYLES = ("paren", "minus")
MONTH_STYLES = ("mon_yy", "mon_yyyy")

_MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def fmt_money(
    value: Decimal | None, style: str, *, neg: str = "paren", zero_dash: bool = False
) -> str:
    """Format money exactly as a document would print it. None prints as blank."""
    if value is None:
        return ""
    if value == 0 and zero_dash:
        return "-"
    magnitude = abs(value)
    if style == "plain":
        text = f"{magnitude:f}"
        if magnitude == magnitude.to_integral_value():
            text = f"{int(magnitude)}"
    elif style == "dollar_commas":
        text = f"${magnitude:,.2f}"
    elif style == "commas":
        text = f"{magnitude:,.2f}"
    else:
        raise ValueError(f"unknown money style {style!r}")
    if value < 0:
        return f"({text})" if neg == "paren" else f"-{text}"
    return text


def fmt_date(value: date | None, style: str) -> str:
    if value is None:
        return ""
    if style == "us_slash":
        return f"{value.month:02d}/{value.day:02d}/{value.year}"
    if style == "iso":
        return value.isoformat()
    if style == "day_mon":
        return f"{value.day:02d}-{_MON[value.month - 1]}-{value.year}"
    raise ValueError(f"unknown date style {style!r}")


def fmt_month(year: int, month: int, style: str) -> str:
    if style == "mon_yy":
        return f"{_MON[month - 1]}-{year % 100:02d}"
    if style == "mon_yyyy":
        return f"{_MON[month - 1]} {year}"
    raise ValueError(f"unknown month style {style!r}")


STATUS_TEXT: dict[str, dict[str, str]] = {
    "plain": {
        "occupied": "Occupied",
        "vacant": "Vacant",
        "notice": "Notice",
        "model": "Model",
        "down": "Down",
        "employee": "Employee",
    },
    "yardi": {
        "occupied": "Current",
        "vacant": "Vacant-Unrented",
        "notice": "NTV",
        "model": "Model",
        "down": "Down",
        "employee": "Employee",
    },
}
