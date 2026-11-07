"""Wiring shared by CLIs: build the LLM client, cost tracker and cache from the config."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from cre_screen.config import DEFAULT_CONFIG_DIR, AppConfig
from cre_screen.mapping.cache import LLMCache
from cre_screen.mapping.cost import CostTracker
from cre_screen.mapping.llm_client import AnthropicLLMClient

REPO_ROOT = DEFAULT_CONFIG_DIR.parent
LEDGER = REPO_ROOT / ".cost_ledger.jsonl"
LEARNED = DEFAULT_CONFIG_DIR / "coa_learned.yaml"


def load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines from a gitignored .env without overriding the shell."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def with_overrides(
    cfg: AppConfig,
    *,
    cache_mode: str | None = None,
    model: str | None = None,
    run_cap: Decimal | None = None,
) -> AppConfig:
    """Return a config copy with CLI overrides applied (cache mode, default model, run cap)."""
    models = cfg.models
    if cache_mode is not None:
        models = models.model_copy(
            update={"cache": models.cache.model_copy(update={"mode": cache_mode})}
        )
    if model is not None:
        models = models.model_copy(update={"default_model": model})
    if run_cap is not None:
        models = models.model_copy(
            update={"budget": models.budget.model_copy(update={"per_run_cap_usd": run_cap})}
        )
    return cfg.model_copy(update={"models": models})


def make_llm(cfg: AppConfig) -> tuple[AnthropicLLMClient, CostTracker, LLMCache]:
    load_dotenv(REPO_ROOT / ".env")
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    cache_dir = cfg.models.cache.dir
    if not cache_dir.is_absolute():
        cache_dir = REPO_ROOT / cache_dir
    cache = LLMCache(cache_dir, mode=cfg.models.cache.mode, version=cfg.models.cache.version)
    tracker = CostTracker(cfg.models, LEDGER, run_id=run_id)
    return AnthropicLLMClient(cfg.models, cache, tracker), tracker, cache
