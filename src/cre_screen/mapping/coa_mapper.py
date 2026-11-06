"""Map T-12 line labels to chart-of-accounts buckets: synonyms first, LLM for the rest."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from rapidfuzz import fuzz

from cre_screen.config import CoaConfig
from cre_screen.errors import ConfigError
from cre_screen.mapping.header_mapper import normalize_header
from cre_screen.mapping.llm_protocol import LLMClient
from cre_screen.mapping.prompts import SYSTEM_LINE, render_prompt
from cre_screen.schemas.common import COABucket
from cre_screen.schemas.mapping import COAMappingBatch

logger = logging.getLogger(__name__)

FUZZY_MIN_SCORE = 92
LLM_CONFIDENCE = 0.7
MappingSource = Literal["synonym_table", "fuzzy", "llm", "manual_override"]


@dataclass(frozen=True)
class LineMapping:
    bucket: COABucket
    is_nonrecurring: bool
    source: MappingSource
    confidence: float


def load_learned(path: Path) -> dict[str, list[str]]:
    """Approved remaps from the UI: ``{BUCKET: [label, ...]}``. Missing file means none."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.as_posix()} must map bucket names to label lists")
    return {str(k): [str(x) for x in v] for k, v in data.items()}


def append_learned(path: Path, bucket: str, label: str) -> None:
    """Record an approved mapping (atomic write)."""
    learned = load_learned(path)
    labels = learned.setdefault(bucket, [])
    if label not in labels:
        labels.append(label)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(yaml.safe_dump(learned, sort_keys=True, allow_unicode=True), encoding="utf-8")
    os.replace(tmp, path)


def subtotal_kind(
    label: str, coa: CoaConfig
) -> Literal["total_income", "total_expenses", "noi"] | None:
    """The StatedSubtotal kind a label names, if it is one of the configured subtotal labels."""
    norm = normalize_header(label)
    for kind, labels in coa.subtotal_labels.model_dump().items():
        if norm in {normalize_header(x) for x in labels}:
            return kind  # type: ignore[no-any-return]
    return None


def has_nonrecurring_keyword(label: str, coa: CoaConfig) -> bool:
    lowered = label.lower()
    return any(k.lower() in lowered for k in coa.nonrecurring_keywords)


def _synonym_index(coa: CoaConfig, learned: dict[str, list[str]]) -> dict[str, str]:
    index: dict[str, str] = {}
    for bucket, cfg in coa.buckets.items():
        for synonym in cfg.synonyms:
            index.setdefault(normalize_header(synonym), bucket)
    for bucket, labels in learned.items():
        for label in labels:
            index[normalize_header(label)] = bucket  # learned mappings win
    return index


def _local_match(label: str, index: dict[str, str]) -> tuple[str, MappingSource, float] | None:
    norm = normalize_header(label)
    if norm in index:
        return index[norm], "synonym_table", 1.0
    # Strict string similarity, not token_set_ratio: token_set scores 100 when one label's words
    # are a subset of another's, so "Insurance Proceeds" would match the synonym "Insurance".
    best_score = 0
    best_bucket = ""
    for synonym, bucket in index.items():
        score = int(fuzz.ratio(norm, synonym))
        if score > best_score:
            best_score, best_bucket = score, bucket
    if best_score >= FUZZY_MIN_SCORE:
        return best_bucket, "fuzzy", best_score / 100
    return None


def _ask_llm(labels: list[str], coa: CoaConfig, llm: LLMClient) -> COAMappingBatch:
    buckets = "\n".join(f"- {name}: {cfg.description}" for name, cfg in coa.buckets.items())
    prompt = render_prompt(
        "coa_map",
        buckets_with_descriptions=buckets,
        labels_json=json.dumps(labels, ensure_ascii=True),
    )
    return llm.structured(
        task="coa_map",
        response_model=COAMappingBatch,
        system=SYSTEM_LINE,
        user_content=[{"type": "text", "text": prompt}],
    )


def map_labels(
    labels: list[str],
    coa: CoaConfig,
    llm: LLMClient,
    *,
    learned: dict[str, list[str]] | None = None,
) -> dict[str, LineMapping]:
    """Map every distinct label. Never raises on a bad LLM answer: the label becomes UNMAPPED."""
    index = _synonym_index(coa, learned or {})
    result: dict[str, LineMapping] = {}
    pending: list[str] = []
    for label in dict.fromkeys(labels):
        local = _local_match(label, index)
        if local is None:
            pending.append(label)
            continue
        bucket, source, confidence = local
        result[label] = LineMapping(
            bucket=bucket,  # type: ignore[arg-type]
            is_nonrecurring=has_nonrecurring_keyword(label, coa),
            source=source,
            confidence=confidence,
        )

    llm_answers: dict[str, tuple[str, bool]] = {}
    to_ask = list(pending)
    for _round in range(2):  # first ask, then one re-ask for anything missing
        if not to_ask:
            break
        batch = _ask_llm(to_ask, coa, llm)
        asked = set(to_ask)
        for mapping in batch.mappings:
            if mapping.line_label not in asked:
                logger.warning("LLM returned unknown label %r; dropped", mapping.line_label)
            elif mapping.line_label not in llm_answers:
                llm_answers[mapping.line_label] = (mapping.bucket, mapping.is_nonrecurring)
        to_ask = [x for x in to_ask if x not in llm_answers]

    for label in pending:
        if label in llm_answers:
            bucket, said_nonrecurring = llm_answers[label]
            result[label] = LineMapping(
                bucket=bucket,  # type: ignore[arg-type]
                is_nonrecurring=said_nonrecurring or has_nonrecurring_keyword(label, coa),
                source="llm",
                confidence=LLM_CONFIDENCE,
            )
        else:
            logger.warning("label %r still unmapped after re-ask", label)
            result[label] = LineMapping(
                bucket="UNMAPPED",
                is_nonrecurring=has_nonrecurring_keyword(label, coa),
                source="llm",
                confidence=0.0,
            )
    return result
