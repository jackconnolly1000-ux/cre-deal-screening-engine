from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from cre_screen.config import ModelsConfig, load_config
from cre_screen.errors import BudgetExceededError, ConfigError
from cre_screen.mapping.cost import CostTracker


@pytest.fixture
def models_cfg() -> ModelsConfig:
    return load_config().models


def make_tracker(
    cfg: ModelsConfig,
    tmp_path: Path,
    *,
    total_cap: str | None = None,
    run_cap: str | None = None,
    run_id: str = "run-1",
) -> CostTracker:
    budget = cfg.budget.model_copy(
        update={
            "total_cap_usd": Decimal(total_cap) if total_cap else cfg.budget.total_cap_usd,
            "per_run_cap_usd": Decimal(run_cap) if run_cap else cfg.budget.per_run_cap_usd,
        }
    )
    return CostTracker(
        cfg.model_copy(update={"budget": budget}), tmp_path / ".cost_ledger.jsonl", run_id=run_id
    )


def test_haiku_short_context_cost(models_cfg: ModelsConfig, tmp_path: Path) -> None:
    t = make_tracker(models_cfg, tmp_path)
    # 1000 * 0.10 / 1e6 + 500 * 0.50 / 1e6
    assert t.cost_usd("claude-haiku-5-5", 1000, 500) == Decimal("0.00035")


def test_haiku_at_threshold_still_short_context(models_cfg: ModelsConfig, tmp_path: Path) -> None:
    t = make_tracker(models_cfg, tmp_path)
    assert t.cost_usd("claude-haiku-5-5", 100_000, 0) == Decimal("0.01")


def test_haiku_long_context_cost(models_cfg: ModelsConfig, tmp_path: Path) -> None:
    t = make_tracker(models_cfg, tmp_path)
    # 150000 * 0.50 / 1e6 + 1000 * 2.50 / 1e6
    assert t.cost_usd("claude-haiku-5-5", 150_000, 1000) == Decimal("0.0775")


def test_haiku_one_token_over_threshold_uses_long_price(
    models_cfg: ModelsConfig, tmp_path: Path
) -> None:
    t = make_tracker(models_cfg, tmp_path)
    assert t.cost_usd("claude-haiku-5-5", 100_001, 0) == Decimal("0.0500005")


def test_sonnet_cost(models_cfg: ModelsConfig, tmp_path: Path) -> None:
    t = make_tracker(models_cfg, tmp_path)
    assert t.cost_usd("claude-sonnet-5-5", 1_000_000, 100_000) == Decimal("3.00")


def test_unknown_model_raises(models_cfg: ModelsConfig, tmp_path: Path) -> None:
    t = make_tracker(models_cfg, tmp_path)
    with pytest.raises(ConfigError):
        t.cost_usd("claude-nope", 1, 1)


def test_worst_case_cost_uses_max_tokens_as_output(
    models_cfg: ModelsConfig, tmp_path: Path
) -> None:
    t = make_tracker(models_cfg, tmp_path)
    assert t.worst_case_cost("claude-haiku-5-5", 1000, 4096) == t.cost_usd(
        "claude-haiku-5-5", 1000, 4096
    )


def test_record_appends_valid_json_lines(models_cfg: ModelsConfig, tmp_path: Path) -> None:
    t = make_tracker(models_cfg, tmp_path)
    t.record(task="header_map", model="claude-haiku-5-5", input_tokens=1000, output_tokens=500)
    t.record(
        task="coa_map", model="claude-haiku-5-5", input_tokens=2000, output_tokens=100, attempt=2
    )
    lines = t.ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert set(first) == {
        "ts",
        "run_id",
        "task",
        "model",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "attempt",
    }
    assert first["run_id"] == "run-1"
    assert first["attempt"] == 1
    assert Decimal(first["cost_usd"]) == Decimal("0.00035")
    assert json.loads(lines[1])["attempt"] == 2


def test_record_updates_run_and_lifetime_totals(models_cfg: ModelsConfig, tmp_path: Path) -> None:
    t = make_tracker(models_cfg, tmp_path)
    t.record(task="header_map", model="claude-haiku-5-5", input_tokens=1000, output_tokens=500)
    assert t.run_total() == Decimal("0.00035")
    assert t.lifetime_total() == Decimal("0.00035")


def test_lifetime_total_includes_other_runs(models_cfg: ModelsConfig, tmp_path: Path) -> None:
    make_tracker(models_cfg, tmp_path, run_id="old").record(
        task="header_map", model="claude-sonnet-5-5", input_tokens=1_000_000, output_tokens=0
    )
    t = make_tracker(models_cfg, tmp_path, run_id="new")
    assert t.run_total() == Decimal("0")
    assert t.lifetime_total() == Decimal("2.00")


def test_missing_ledger_means_zero_spend(models_cfg: ModelsConfig, tmp_path: Path) -> None:
    t = make_tracker(models_cfg, tmp_path)
    assert t.lifetime_total() == Decimal("0")


def test_per_run_cap_raises_when_worst_case_exceeds(
    models_cfg: ModelsConfig, tmp_path: Path
) -> None:
    t = make_tracker(models_cfg, tmp_path, run_cap="1.00")
    with pytest.raises(BudgetExceededError, match="run"):
        t.check_can_spend(Decimal("1.01"))


def test_per_run_cap_allows_exactly_the_cap(models_cfg: ModelsConfig, tmp_path: Path) -> None:
    t = make_tracker(models_cfg, tmp_path, run_cap="1.00")
    t.check_can_spend(Decimal("1.00"))


def test_per_run_cap_counts_spend_already_recorded(
    models_cfg: ModelsConfig, tmp_path: Path
) -> None:
    t = make_tracker(models_cfg, tmp_path, run_cap="1.00")
    t.record(task="coa_map", model="claude-sonnet-5-5", input_tokens=400_000, output_tokens=0)
    t.check_can_spend(Decimal("0.20"))
    with pytest.raises(BudgetExceededError, match="run"):
        t.check_can_spend(Decimal("0.21"))


def test_lifetime_cap_raises_across_runs(models_cfg: ModelsConfig, tmp_path: Path) -> None:
    make_tracker(models_cfg, tmp_path, run_id="old").record(
        task="coa_map", model="claude-sonnet-5-5", input_tokens=4_000_000, output_tokens=0
    )  # $8.00
    t = make_tracker(models_cfg, tmp_path, total_cap="10.00", run_cap="5.00", run_id="new")
    t.check_can_spend(Decimal("2.00"))
    with pytest.raises(BudgetExceededError, match="lifetime"):
        t.check_can_spend(Decimal("2.01"))


def test_ledger_directory_is_created_on_first_write(
    models_cfg: ModelsConfig, tmp_path: Path
) -> None:
    cfg = models_cfg
    t = CostTracker(cfg, tmp_path / "sub" / "ledger.jsonl", run_id="r")
    t.record(task="concession", model="claude-haiku-5-5", input_tokens=10, output_tokens=10)
    assert t.ledger_path.exists()
