"""Map rent roll column headers to fields: synonym table first, LLM only as a fallback."""

from __future__ import annotations

import json
import logging
import re

from pydantic import ValidationError
from rapidfuzz import fuzz

from cre_screen.config import RRHeadersConfig
from cre_screen.errors import HeaderMapError
from cre_screen.mapping.llm_protocol import LLMClient
from cre_screen.mapping.prompts import SYSTEM_LINE, render_prompt
from cre_screen.schemas.mapping import LLMHeaderMap
from cre_screen.schemas.rent_roll import ColumnMap, HeaderMap

logger = logging.getLogger(__name__)

FIELD_DESCRIPTIONS: dict[str, str] = {
    "unit_id": "the unit or apartment number",
    "unit_type": "floor plan or unit type label",
    "beds": "bedroom count",
    "baths": "bathroom count",
    "sq_ft": "square footage",
    "status": "occupancy status of the unit",
    "tenant_name": "tenant or resident name",
    "in_place_rent": "rent the tenant currently pays (contract rent)",
    "market_rent": "asking or market rent for the unit",
    "lease_start": "lease start or move-in date",
    "lease_end": "lease end or expiration date",
    "concession_note": "free-text notes or concessions",
}


def normalize_header(text: str) -> str:
    """Lowercase, strip punctuation, collapse spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def match_headers(headers: list[str], cfg: RRHeadersConfig) -> dict[str, str]:
    """Return {header_text: field} using the synonym table.

    Candidates are scored with ``token_set_ratio``; an exact normalized match always beats a
    fuzzy one (``token_set_ratio`` scores 100 for any token subset, so "Rent" would otherwise tie
    with "Market Rent"). Each header and each field is used once, best candidate first.
    """
    candidates: list[tuple[int, int, int, str, str]] = []  # (-exact, -score, index, header, field)
    for index, header in enumerate(headers):
        norm = normalize_header(header)
        if not norm:
            continue
        for field, synonyms in cfg.fields.items():
            best_exact = 0
            best_score = 0
            for synonym in synonyms:
                syn_norm = normalize_header(synonym)
                if syn_norm == norm:
                    best_exact = 1
                    best_score = 100
                    break
                best_score = max(best_score, int(fuzz.token_set_ratio(norm, syn_norm)))
            if best_exact or best_score >= cfg.fuzzy_min_score:
                candidates.append((-best_exact, -best_score, index, header, field))
    candidates.sort()
    used_headers: set[int] = set()
    used_fields: set[str] = set()
    result: dict[str, str] = {}
    for _exact, _score, index, header, field in candidates:
        if index in used_headers or field in used_fields:
            continue
        used_headers.add(index)
        used_fields.add(field)
        result[header] = field
    return result


def _from_synonyms(headers: list[str], cfg: RRHeadersConfig) -> HeaderMap | None:
    matched = match_headers(headers, cfg)
    columns = [ColumnMap(header_text=h, maps_to=matched.get(h, "ignore")) for h in headers]  # type: ignore[arg-type]
    try:
        return HeaderMap(columns=columns, source="synonym_table")
    except ValidationError:
        return None


def _ask_llm(
    headers: list[str],
    sample_rows: list[dict[str, str]],
    llm: LLMClient,
    problem: str | None,
) -> LLMHeaderMap:
    fields = "; ".join(f"{name} ({desc})" for name, desc in FIELD_DESCRIPTIONS.items())
    prompt = render_prompt(
        "header_map",
        fields_with_descriptions=fields,
        headers_json=json.dumps(headers, ensure_ascii=True),
        sample_rows_json=json.dumps(sample_rows, ensure_ascii=True),
    )
    if problem:
        prompt += f"\n\nYour previous answer was rejected: {problem}. Fix it."
    return llm.structured(
        task="header_map",
        response_model=LLMHeaderMap,
        system=SYSTEM_LINE,
        user_content=[{"type": "text", "text": prompt}],
    )


def _to_header_map(headers: list[str], response: LLMHeaderMap, warnings: list[str]) -> HeaderMap:
    given = set(headers)
    seen: dict[str, str] = {}
    for col in response.columns:
        if col.header_text not in given:
            warnings.append(f"LLM returned unknown header {col.header_text!r}; dropped")
        elif col.header_text in seen:
            warnings.append(f"LLM returned header {col.header_text!r} twice; kept first")
        else:
            seen[col.header_text] = col.maps_to
    for header in headers:
        if header not in seen:
            warnings.append(f"LLM omitted header {header!r}; treated as ignore")
    columns = [ColumnMap(header_text=h, maps_to=seen.get(h, "ignore")) for h in headers]  # type: ignore[arg-type]
    return HeaderMap(columns=columns, source="llm")


def map_headers(
    headers: list[str],
    sample_rows: list[dict[str, str]],
    cfg: RRHeadersConfig,
    llm: LLMClient,
) -> tuple[HeaderMap, list[str]]:
    """Return the HeaderMap and any warnings. Raises HeaderMapError if nothing valid results."""
    synonym_map = _from_synonyms(headers, cfg)
    if synonym_map is not None:
        return synonym_map, []

    warnings: list[str] = []
    problem: str | None = None
    for _attempt in range(2):
        response = _ask_llm(headers, sample_rows[:3], llm, problem)
        try:
            return _to_header_map(headers, response, warnings), warnings
        except ValidationError as exc:
            problem = "; ".join(str(e["msg"]) for e in exc.errors())
            logger.warning("LLM header map rejected: %s", problem)
    raise HeaderMapError(f"header mapping failed validation after retry: {problem}")
