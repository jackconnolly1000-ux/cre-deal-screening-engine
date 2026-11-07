"""Pure parsing functions for printed values. No I/O, no LLM."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from cre_screen.config import RRHeadersConfig
from cre_screen.errors import DateParseError, MoneyParseError, NumberParseError
from cre_screen.schemas.common import Status, to_cents

_NBSP = chr(0xA0)  # non-breaking space
_DASHES = {"-", chr(0x2013), chr(0x2014)}  # hyphen, en dash, em dash
_MINUS = chr(0x2212)  # Unicode minus sign
_NUMBER = re.compile(r"^(\d+(?:\.\d+)?|\.\d+)$")
_BLANKS = {"", "n/a", "mtm", "m-t-m", "m2m", "month to month", "month-to-month"}

_DATE_FORMATS = (
    "%m/%d/%Y",
    "%m/%d/%y",
    "%Y-%m-%d",
    "%d-%b-%Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
)
_MONTH_FORMATS = ("%b-%y", "%b %Y", "%m/%Y")


def _clean(text: str) -> str:
    return text.replace(_NBSP, " ").strip()


def _split_sign(body: str, original: str) -> tuple[str, bool]:
    """Return (digits, negative) from text with $ and commas already removed."""
    negative_marks = 0
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1].strip()
        negative_marks += 1
    if body and body[0] in ("-", _MINUS):
        body = body[1:].strip()
        negative_marks += 1
    if body and body[-1] in ("-", _MINUS):
        body = body[:-1].strip()
        negative_marks += 1
    if negative_marks > 1 or not _NUMBER.match(body):
        raise MoneyParseError(original)
    return body, negative_marks == 1


def parse_money(text: str) -> Decimal | None:
    original = text
    cleaned = _clean(text).replace("$", "").replace(",", "").strip()
    if cleaned.lower() in _BLANKS:
        return None
    if cleaned in _DASHES:
        return Decimal("0.00")
    digits, negative = _split_sign(cleaned, original)
    value = Decimal(digits)
    if negative and value != 0:
        value = -value
    return to_cents(value)


def money_was_rounded(text: str) -> bool:
    """True when the printed amount has more than two decimal places."""
    cleaned = _clean(text).replace("$", "").replace(",", "")
    match = re.search(r"\.(\d+)", cleaned)
    return bool(match) and len(match.group(1)) > 2 and bool(match.group(1)[2:].strip("0"))


def parse_decimal(text: str) -> Decimal | None:
    cleaned = _clean(text).replace(",", "")
    if cleaned.lower() in _BLANKS:
        return None
    if not re.match(r"^-?(\d+(?:\.\d+)?|\.\d+)$", cleaned):
        raise NumberParseError(text)
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise NumberParseError(text) from exc


def parse_int(text: str) -> int | None:
    value = parse_decimal(text)
    if value is None:
        return None
    if value != value.to_integral_value():
        raise NumberParseError(text)
    return int(value)


def _last_day(year: int, month: int) -> date:
    first_next = date(year + (month == 12), month % 12 + 1, 1)
    return date.fromordinal(first_next.toordinal() - 1)


def parse_date(text: str | date | datetime, *, end_of_month: bool = False) -> date | None:
    if isinstance(text, datetime):
        return text.date()
    if isinstance(text, date):
        return text
    cleaned = _clean(text)
    if cleaned.lower() in _BLANKS or cleaned in _DASHES:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    for fmt in _MONTH_FORMATS:
        try:
            parsed = datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
        return _last_day(parsed.year, parsed.month) if end_of_month else parsed.replace(day=1)
    raise DateParseError(text)


def normalize_status(
    text: str | None, tenant: str | None, rent: Decimal | None, cfg: RRHeadersConfig
) -> Status:
    """Map printed status text to a Status. Unknown text gives ``unknown`` (caller flags it)."""
    if text is not None and text.strip():
        lowered = text.strip().lower()
        synonyms = cfg.status_synonyms.model_dump()
        for status, words in synonyms.items():
            if lowered in {w.lower() for w in words}:
                return status  # type: ignore[no-any-return]
        return "unknown"
    markers = {m.upper() for m in cfg.vacant_tenant_markers}
    tenant_text = (tenant or "").strip()
    if tenant_text.upper() in markers or (not tenant_text and rent is None):
        return "vacant"
    return "occupied"
