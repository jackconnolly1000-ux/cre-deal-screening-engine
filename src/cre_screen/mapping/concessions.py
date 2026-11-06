"""Parse free-text concession notes. Every number must be grounded in the note's text."""

from __future__ import annotations

import logging
import re
from decimal import Decimal

from pydantic import ValidationError

from cre_screen.config import RRConcessionCfg
from cre_screen.mapping.llm_protocol import LLMClient
from cre_screen.mapping.prompts import SYSTEM_LINE, render_prompt
from cre_screen.schemas.citation import Citation
from cre_screen.schemas.common import to_cents
from cre_screen.schemas.mapping import ConcessionParse
from cre_screen.schemas.rent_roll import Concession

logger = logging.getLogger(__name__)

FREE_MONTHS = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:mo|month)s?\s*free(?:\s*on\s*(\d+)\s*-?\s*(?:mo|month))?",
    re.IGNORECASE,
)
MONTHLY_DISCOUNT = re.compile(
    r"\$?(\d[\d,]*(?:\.\d+)?)\s*(?:off|discount)\s*(?:/|per)?\s*mo", re.IGNORECASE
)


def plain_number(value: Decimal | int) -> str:
    """Plain string form with no trailing zeros: 1, 13, 150, 1.5."""
    text = format(Decimal(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def is_grounded(value: Decimal | int, note: str) -> bool:
    """True when the number appears in the note (after removing $ and commas) as a whole number."""
    cleaned = note.replace("$", "").replace(",", "")
    pattern = rf"(?<![\d.]){re.escape(plain_number(value))}(?!\.?\d)"
    return re.search(pattern, cleaned) is not None


def monthly_equivalent(
    kind: str,
    *,
    in_place_rent: Decimal | None,
    months_free: Decimal | None,
    monthly_discount: Decimal | None,
    one_time_credit: Decimal | None,
    term: int,
) -> Decimal:
    if kind == "free_months" and months_free is not None and in_place_rent is not None:
        return to_cents(in_place_rent * months_free / term)
    if kind == "monthly_discount" and monthly_discount is not None:
        return to_cents(monthly_discount)
    if kind == "one_time_credit" and one_time_credit is not None:
        return to_cents(one_time_credit / term)
    return to_cents(Decimal(0))


def _regex_path(
    note: str, in_place_rent: Decimal | None, citation: Citation, cfg: RRConcessionCfg
) -> list[Concession]:
    found: list[Concession] = []
    for match in FREE_MONTHS.finditer(note):
        months = Decimal(match.group(1))
        term = int(match.group(2)) if match.group(2) else cfg.default_lease_term_months
        found.append(
            Concession(
                kind="free_months",
                monthly_equivalent=monthly_equivalent(
                    "free_months",
                    in_place_rent=in_place_rent,
                    months_free=months,
                    monthly_discount=None,
                    one_time_credit=None,
                    term=term,
                ),
                months_free=months,
                lease_term_months=term,
                raw_text=note,
                grounded=True,
                citation=citation,
            )
        )
    for match in MONTHLY_DISCOUNT.finditer(note):
        amount = to_cents(Decimal(match.group(1).replace(",", "")))
        found.append(
            Concession(
                kind="monthly_discount",
                monthly_equivalent=to_cents(amount),
                monthly_discount=amount,
                raw_text=note,
                grounded=True,
                citation=citation,
            )
        )
    return found


def _ungrounded(note: str, citation: Citation) -> Concession:
    return Concession(
        kind="other",
        monthly_equivalent=to_cents(Decimal(0)),
        raw_text=note,
        grounded=False,
        citation=citation,
    )


def _from_llm(
    note: str,
    in_place_rent: Decimal | None,
    citation: Citation,
    cfg: RRConcessionCfg,
    llm: LLMClient,
) -> Concession:
    parsed = llm.structured(
        task="concession",
        response_model=ConcessionParse,
        system=SYSTEM_LINE,
        user_content=[{"type": "text", "text": render_prompt("concession", note=note)}],
    )
    numbers: list[Decimal | int] = [
        v
        for v in (
            parsed.months_free,
            parsed.monthly_discount,
            parsed.one_time_credit,
            parsed.lease_term_months,
        )
        if v is not None
    ]
    if not all(is_grounded(v, note) for v in numbers):
        logger.info("concession note failed grounding: %r", note)
        return _ungrounded(note, citation)
    term = parsed.lease_term_months or cfg.default_lease_term_months
    try:
        return Concession(
            kind=parsed.kind,
            monthly_equivalent=monthly_equivalent(
                parsed.kind,
                in_place_rent=in_place_rent,
                months_free=parsed.months_free,
                monthly_discount=parsed.monthly_discount,
                one_time_credit=parsed.one_time_credit,
                term=term,
            ),
            months_free=parsed.months_free,
            monthly_discount=None
            if parsed.monthly_discount is None
            else to_cents(parsed.monthly_discount),
            one_time_credit=None
            if parsed.one_time_credit is None
            else to_cents(parsed.one_time_credit),
            lease_term_months=parsed.lease_term_months
            or (term if parsed.kind in ("free_months", "one_time_credit") else None),
            raw_text=note,
            grounded=True,
            citation=citation,
        )
    except ValidationError:
        logger.info("concession values out of range: %r", note)
        return _ungrounded(note, citation)


def parse_concessions(
    note: str,
    *,
    in_place_rent: Decimal | None,
    citation: Citation,
    cfg: RRConcessionCfg,
    llm: LLMClient,
) -> list[Concession]:
    """Regex fast path (no LLM); otherwise one grounded LLM parse. Blank notes give []."""
    text = note.strip()
    if not text:
        return []
    found = _regex_path(text, in_place_rent, citation, cfg)
    if found:
        return found
    return [_from_llm(text, in_place_rent, citation, cfg, llm)]
