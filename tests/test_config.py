from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path
from typing import get_args

import pytest
import yaml

from cre_screen.config import DEFAULT_CONFIG_DIR, load_config
from cre_screen.errors import ConfigError
from cre_screen.schemas.common import COABucket

CONFIG_FILES = ["coa.yaml", "rr_headers.yaml", "thresholds.yaml", "models.yaml"]


@pytest.fixture
def config_copy(tmp_path: Path) -> Path:
    dest = tmp_path / "config"
    shutil.copytree(DEFAULT_CONFIG_DIR, dest)
    return dest


def edit_yaml(path: Path, mutate) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_all_four_files_load() -> None:
    cfg = load_config()
    assert cfg.coa and cfg.rr_headers and cfg.thresholds and cfg.models


def test_coa_has_the_20_buckets_in_schema_order() -> None:
    cfg = load_config()
    assert list(cfg.coa.buckets) == list(get_args(COABucket))
    assert len(cfg.coa.buckets) == 20


def test_thresholds_load_as_decimal() -> None:
    cfg = load_config()
    assert cfg.thresholds.rr_footing.tolerance_usd == Decimal("1.00")
    assert isinstance(cfg.thresholds.t12_spike.factor, Decimal)
    assert cfg.thresholds.pf_ins_drop.drop_pct == Decimal("0.10")


def test_pricing_loads_as_decimal() -> None:
    price = load_config().models.pricing_usd_per_mtok["claude-haiku-5-5"]
    assert price.input == Decimal("0.10")
    assert price.input_long == Decimal("0.50")
    assert price.long_context_threshold == 100_000


@pytest.mark.parametrize("name", CONFIG_FILES)
def test_unknown_top_level_key_fails(config_copy: Path, name: str) -> None:
    edit_yaml(config_copy / name, lambda d: d.update({"surprise_key": 1}))
    with pytest.raises(ConfigError, match=name):
        load_config(config_copy)


def test_unknown_nested_key_fails(config_copy: Path) -> None:
    edit_yaml(
        config_copy / "thresholds.yaml",
        lambda d: d["rr_footing"].update({"tolerence_usd": "1.00"}),
    )
    with pytest.raises(ConfigError, match="tolerence_usd"):
        load_config(config_copy)


def test_missing_file_fails(config_copy: Path) -> None:
    (config_copy / "coa.yaml").unlink()
    with pytest.raises(ConfigError, match=r"coa\.yaml"):
        load_config(config_copy)


def test_cache_mode_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRE_LLM_CACHE_MODE", "record")
    assert load_config().models.cache.mode == "record"


def test_cache_mode_defaults_to_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRE_LLM_CACHE_MODE", raising=False)
    assert load_config().models.cache.mode == "replay"


def test_bad_cache_mode_env_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRE_LLM_CACHE_MODE", "yolo")
    with pytest.raises(ConfigError, match="CRE_LLM_CACHE_MODE"):
        load_config()
