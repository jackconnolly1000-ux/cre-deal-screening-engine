from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from cre_screen.errors import CacheMissError, ConfigError
from cre_screen.mapping.cache import LLMCache


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


class OtherAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str
    extra_field: int = 0


class IntAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


USER = [{"type": "text", "text": "hello"}]


def key_for(cache: LLMCache, **overrides: object) -> str:
    args: dict[str, object] = {
        "model": "claude-haiku-5-5",
        "task": "header_map",
        "system": "sys",
        "user_content": USER,
        "response_model": Answer,
        "temperature": 0,
        "max_tokens": 1024,
    }
    args.update(overrides)
    return cache.make_key(**args)  # type: ignore[arg-type]


def put_answer(cache: LLMCache, key: str, value: str = "x") -> None:
    cache.put(
        key=key,
        task="header_map",
        model="claude-haiku-5-5",
        response=Answer(value=value),
        input_tokens=10,
        output_tokens=5,
        attempts=1,
        cost_usd="0.00001",
    )


def test_key_is_stable_across_instances(tmp_path: Path) -> None:
    a = LLMCache(tmp_path, mode="replay", version=1)
    b = LLMCache(tmp_path / "other", mode="record", version=1)
    assert key_for(a) == key_for(b)
    assert len(key_for(a)) == 64


def test_key_ignores_dict_key_order(tmp_path: Path) -> None:
    c = LLMCache(tmp_path, mode="replay", version=1)
    k1 = key_for(c, user_content=[{"type": "text", "text": "hi"}])
    k2 = key_for(c, user_content=[{"text": "hi", "type": "text"}])
    assert k1 == k2


@pytest.mark.parametrize(
    "change",
    [
        {"model": "claude-sonnet-5-5"},
        {"task": "coa_map"},
        {"system": "other"},
        {"user_content": [{"type": "text", "text": "bye"}]},
        {"response_model": OtherAnswer},
        {"temperature": 1},
        {"max_tokens": 2048},
    ],
)
def test_key_changes_when_any_input_changes(tmp_path: Path, change: dict[str, object]) -> None:
    c = LLMCache(tmp_path, mode="replay", version=1)
    assert key_for(c, **change) != key_for(c)


def test_key_changes_with_cache_version(tmp_path: Path) -> None:
    assert key_for(LLMCache(tmp_path, mode="replay", version=1)) != key_for(
        LLMCache(tmp_path, mode="replay", version=2)
    )


def test_replay_miss_raises_cache_miss_error(tmp_path: Path) -> None:
    c = LLMCache(tmp_path, mode="replay", version=1)
    key = key_for(c)
    with pytest.raises(CacheMissError) as info:
        c.require(key, task="header_map", response_model=Answer)
    assert info.value.key == key
    assert info.value.task == "header_map"


def test_get_returns_none_on_miss(tmp_path: Path) -> None:
    c = LLMCache(tmp_path, mode="record", version=1)
    assert c.get(key_for(c), Answer) is None


def test_record_then_replay_returns_validated_model(tmp_path: Path) -> None:
    rec = LLMCache(tmp_path, mode="record", version=1)
    key = key_for(rec)
    put_answer(rec, key, "mapped")
    rep = LLMCache(tmp_path, mode="replay", version=1)
    hit = rep.get(key, Answer)
    assert isinstance(hit, Answer)
    assert hit.value == "mapped"


def test_entry_layout_and_fields(tmp_path: Path) -> None:
    c = LLMCache(tmp_path, mode="record", version=1)
    key = key_for(c)
    put_answer(c, key)
    path = tmp_path / key[:2] / f"{key}.json"
    assert path.exists()
    entry = json.loads(path.read_text(encoding="utf-8"))
    assert set(entry) == {
        "key",
        "task",
        "model",
        "created_at",
        "response",
        "usage",
        "cost_usd",
    }
    assert entry["key"] == key
    assert entry["response"] == {"value": "x"}
    assert entry["usage"] == {"input_tokens": 10, "output_tokens": 5, "attempts": 1}


def test_record_write_is_atomic_and_leaves_no_temp_file(tmp_path: Path) -> None:
    c = LLMCache(tmp_path, mode="record", version=1)
    key = key_for(c)
    put_answer(c, key)
    assert list(tmp_path.rglob("*.tmp")) == []


def test_put_overwrites_existing_entry(tmp_path: Path) -> None:
    c = LLMCache(tmp_path, mode="record", version=1)
    key = key_for(c)
    put_answer(c, key, "first")
    put_answer(c, key, "second")
    got = c.get(key, Answer)
    assert got is not None
    assert got.value == "second"


def test_put_is_refused_outside_record_mode(tmp_path: Path) -> None:
    c = LLMCache(tmp_path, mode="replay", version=1)
    with pytest.raises(ConfigError):
        put_answer(c, key_for(c))


def test_off_mode_never_hits(tmp_path: Path) -> None:
    rec = LLMCache(tmp_path, mode="record", version=1)
    key = key_for(rec)
    put_answer(rec, key)
    off = LLMCache(tmp_path, mode="off", version=1)
    assert off.get(key, Answer) is None


def test_hit_that_fails_schema_validation_raises(tmp_path: Path) -> None:
    c = LLMCache(tmp_path, mode="record", version=1)
    key = key_for(c)
    put_answer(c, key)
    with pytest.raises(ValidationError):
        c.get(key, IntAnswer)
