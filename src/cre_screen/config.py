"""Loads and validates the four YAML config files into Pydantic models.

Every model uses ``extra="forbid"``, so a typo in a key is a startup error.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import ConfigError

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

CACHE_MODE_ENV = "CRE_LLM_CACHE_MODE"

_M = TypeVar("_M", bound=BaseModel)


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- coa.yaml ----------


class BucketCfg(_Cfg):
    section: Literal["revenue", "opex", "below_noi", "excluded", "control"]
    sign: Literal["positive", "negative"]
    description: str
    synonyms: list[str]


class SubtotalLabels(_Cfg):
    total_income: list[str]
    total_expenses: list[str]
    noi: list[str]


class CoaConfig(_Cfg):
    buckets: dict[str, BucketCfg]
    nonrecurring_keywords: list[str]
    subtotal_labels: SubtotalLabels


# ---------- rr_headers.yaml ----------


class StatusSynonyms(_Cfg):
    occupied: list[str]
    vacant: list[str]
    notice: list[str]
    model: list[str]
    down: list[str]
    employee: list[str]


class RRHeadersConfig(_Cfg):
    fuzzy_min_score: int = Field(ge=0, le=100)
    required_fields: list[str]
    status_or_tenant_required: bool
    fields: dict[str, list[str]]
    status_synonyms: StatusSynonyms
    vacant_tenant_markers: list[str]


# ---------- thresholds.yaml ----------


class RRFootingCfg(_Cfg):
    tolerance_usd: Decimal
    market_rent_severity: Literal["block", "warn", "info"]
    max_reextract_attempts: int = Field(ge=0)


class RRZeroRentOccCfg(_Cfg):
    occupied_like_statuses: list[str]


class RRExpiredLeaseCfg(_Cfg):
    occupied_like_statuses: list[str]
    treat_missing_lease_end_as_mtm: bool


class RRConcessionCfg(_Cfg):
    drag_warn_pct: Decimal
    default_lease_term_months: int = Field(ge=1)


class RRRentOutlierCfg(_Cfg):
    deviation_pct: Decimal
    min_group_size: int = Field(ge=1)


class XRRVsT3Cfg(_Cfg):
    gap_warn_pct: Decimal
    occupied_like_statuses: list[str]
    t3_rental_buckets: list[str]


class T12FootingCfg(_Cfg):
    line_tolerance_usd: Decimal
    subtotal_tolerance_usd: Decimal


class T12SpikeCfg(_Cfg):
    factor: Decimal
    min_abs_usd: Decimal
    exempt_buckets: list[str]
    checked_sections: list[str]


class PFDropCfg(_Cfg):
    drop_pct: Decimal


class PFVacancyCfg(_Cfg):
    margin: Decimal
    vacant_like_statuses: list[str]


class PFMgmtFeeCfg(_Cfg):
    margin: Decimal


class ReviewCfg(_Cfg):
    override_note_min_chars: int = Field(ge=1)


class ThresholdsConfig(_Cfg):
    rr_footing: RRFootingCfg
    rr_zero_rent_occ: RRZeroRentOccCfg
    rr_expired_lease: RRExpiredLeaseCfg
    rr_concession: RRConcessionCfg
    rr_rent_outlier: RRRentOutlierCfg
    x_rr_vs_t3: XRRVsT3Cfg
    t12_footing: T12FootingCfg
    t12_spike: T12SpikeCfg
    pf_tax_drop: PFDropCfg
    pf_ins_drop: PFDropCfg
    pf_vacancy: PFVacancyCfg
    pf_mgmt_fee: PFMgmtFeeCfg
    review: ReviewCfg


# ---------- models.yaml ----------


class ModelPrice(_Cfg):
    input: Decimal
    output: Decimal
    long_context_threshold: int | None = Field(default=None, ge=1)
    input_long: Decimal | None = None
    output_long: Decimal | None = None


class MaxTokensCfg(_Cfg):
    header_map: int = Field(ge=1)
    coa_map: int = Field(ge=1)
    concession: int = Field(ge=1)
    vision_page: int = Field(ge=1)


class BudgetCfg(_Cfg):
    total_cap_usd: Decimal
    per_run_cap_usd: Decimal


class CacheCfg(_Cfg):
    dir: Path
    mode: Literal["replay", "record", "off"]
    version: int = Field(ge=1)


class ModelsConfig(_Cfg):
    provider: Literal["anthropic"]
    default_model: str
    comparison_model: str
    temperature: float = Field(ge=0.0)
    max_retries: int = Field(ge=0)
    max_tokens: MaxTokensCfg
    max_prompt_tokens: int = Field(ge=1)
    chars_per_token_estimate: int = Field(ge=1)
    pricing_usd_per_mtok: dict[str, ModelPrice]
    budget: BudgetCfg
    cache: CacheCfg


class AppConfig(_Cfg):
    coa: CoaConfig
    rr_headers: RRHeadersConfig
    thresholds: ThresholdsConfig
    models: ModelsConfig


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path.as_posix()}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path.as_posix()}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.as_posix()} must contain a mapping at the top level")
    return data


def _validate(model: type[_M], path: Path) -> _M:
    data = _read_yaml(path)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path.as_posix()}: {exc}") from exc


def load_config(config_dir: Path | None = None) -> AppConfig:
    """Load all four config files. ``CRE_LLM_CACHE_MODE`` overrides ``cache.mode``."""
    base = config_dir if config_dir is not None else DEFAULT_CONFIG_DIR
    models = _validate(ModelsConfig, base / "models.yaml")
    override = os.environ.get(CACHE_MODE_ENV)
    if override:
        if override not in ("replay", "record", "off"):
            raise ConfigError(f"{CACHE_MODE_ENV} must be replay, record or off (got {override!r})")
        models = models.model_copy(
            update={"cache": models.cache.model_copy(update={"mode": override})}
        )
    return AppConfig(
        coa=_validate(CoaConfig, base / "coa.yaml"),
        rr_headers=_validate(RRHeadersConfig, base / "rr_headers.yaml"),
        thresholds=_validate(ThresholdsConfig, base / "thresholds.yaml"),
        models=models,
    )
