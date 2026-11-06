from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from cre_screen.config import ModelsConfig, load_config
from cre_screen.errors import (
    BudgetExceededError,
    CacheMissError,
    ConfigError,
    PromptTooLargeError,
)
from cre_screen.mapping.cache import LLMCache
from cre_screen.mapping.cost import CostTracker
from cre_screen.mapping.llm_client import AnthropicLLMClient, estimate_prompt_tokens

D = Decimal


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


class OtherAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str
    flag: bool = False


TEXT = [{"type": "text", "text": "map these headers"}]


class FakeInstructor:
    """Stands in for ``instructor.from_anthropic(...)``.

    Each scripted call is ``(result_or_exception, [(input_tokens, output_tokens), ...])``: the
    usage list is replayed through the ``completion:response`` hook, once per attempt.
    """

    def __init__(self, *script: tuple[Any, list[tuple[int, int]]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.handlers: dict[str, Any] = {}
        self.messages = SimpleNamespace(create=self._create)

    def on(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result, attempts = self.script.pop(0)
        for tokens_in, tokens_out in attempts:
            usage = SimpleNamespace(input_tokens=tokens_in, output_tokens=tokens_out)
            self.handlers["completion:response"](SimpleNamespace(usage=usage))
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def models() -> ModelsConfig:
    return load_config().models


def build(
    models: ModelsConfig,
    tmp_path: Path,
    fake: FakeInstructor,
    *,
    mode: str = "record",
    run_cap: str | None = None,
    total_cap: str | None = None,
    run_id: str = "run-1",
) -> tuple[AnthropicLLMClient, CostTracker, LLMCache]:
    budget = models.budget.model_copy(
        update={
            "per_run_cap_usd": D(run_cap) if run_cap else models.budget.per_run_cap_usd,
            "total_cap_usd": D(total_cap) if total_cap else models.budget.total_cap_usd,
        }
    )
    cfg = models.model_copy(update={"budget": budget})
    cache = LLMCache(tmp_path / "cache", mode=mode, version=cfg.cache.version)  # type: ignore[arg-type]
    tracker = CostTracker(cfg, tmp_path / "ledger.jsonl", run_id=run_id)
    return AnthropicLLMClient(cfg, cache, tracker, instructor_client=fake), tracker, cache


def ledger(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def call(client: AnthropicLLMClient, model_cls: type[BaseModel] = Answer, **kw: Any) -> Any:
    return client.structured(
        task=kw.pop("task", "header_map"),
        response_model=model_cls,
        system=kw.pop("system", "sys"),
        user_content=kw.pop("user_content", TEXT),
        **kw,
    )


# ---------- the live path ----------


def test_live_call_passes_config_values_to_instructor(models: ModelsConfig, tmp_path: Path) -> None:
    fake = FakeInstructor((Answer(value="a"), [(100, 20)]))
    client, _, _ = build(models, tmp_path, fake)
    assert call(client).value == "a"
    (sent,) = fake.calls
    assert sent["model"] == models.default_model == "claude-haiku-5-5"
    assert sent["max_tokens"] == models.max_tokens.header_map
    assert sent["temperature"] == 0
    assert sent["max_retries"] == models.max_retries
    assert sent["system"] == "sys"
    assert sent["messages"] == [{"role": "user", "content": TEXT}]
    assert sent["response_model"] is Answer


def test_model_override_and_per_task_max_tokens(models: ModelsConfig, tmp_path: Path) -> None:
    fake = FakeInstructor((Answer(value="a"), [(100, 20)]))
    client, _, _ = build(models, tmp_path, fake)
    call(client, task="coa_map", model="claude-sonnet-5-5")
    assert fake.calls[0]["model"] == "claude-sonnet-5-5"
    assert fake.calls[0]["max_tokens"] == models.max_tokens.coa_map
    assert ledger(tmp_path)[0]["model"] == "claude-sonnet-5-5"


def test_every_attempt_is_recorded_not_only_the_last(models: ModelsConfig, tmp_path: Path) -> None:
    fake = FakeInstructor((Answer(value="a"), [(1000, 200), (1100, 150)]))
    client, tracker, _ = build(models, tmp_path, fake, run_id="r9")
    call(client)
    rows = ledger(tmp_path)
    assert [(r["attempt"], r["input_tokens"], r["output_tokens"]) for r in rows] == [
        (1, 1000, 200),
        (2, 1100, 150),
    ]
    assert {r["run_id"] for r in rows} == {"r9"} and {r["task"] for r in rows} == {"header_map"}
    expected = D("2100") * D("0.10") / 1_000_000 + D("350") * D("0.50") / 1_000_000
    assert tracker.run_total() == expected
    assert sum(D(r["cost_usd"]) for r in rows) == expected


def test_failed_call_still_records_the_attempts_that_cost_money(
    models: ModelsConfig, tmp_path: Path
) -> None:
    fake = FakeInstructor((RuntimeError("validation failed"), [(500, 50), (500, 60), (500, 70)]))
    client, tracker, _cache = build(models, tmp_path, fake)
    with pytest.raises(RuntimeError, match="validation failed"):
        call(client)
    assert [r["attempt"] for r in ledger(tmp_path)] == [1, 2, 3]
    assert tracker.run_total() > 0
    assert not list((tmp_path / "cache").rglob("*.json")), "failures are never cached"


def test_record_mode_writes_an_atomic_cache_entry_with_usage_and_cost(
    models: ModelsConfig, tmp_path: Path
) -> None:
    fake = FakeInstructor((Answer(value="a"), [(1000, 200), (1100, 150)]))
    client, tracker, _cache = build(models, tmp_path, fake)
    call(client)
    (entry_path,) = (tmp_path / "cache").rglob("*.json")
    entry = json.loads(entry_path.read_text("utf-8"))
    assert entry["response"] == {"value": "a"}
    assert entry["task"] == "header_map" and entry["model"] == "claude-haiku-5-5"
    assert entry["usage"] == {"input_tokens": 2100, "output_tokens": 350, "attempts": 2}
    assert D(entry["cost_usd"]) == tracker.run_total()
    assert not list((tmp_path / "cache").rglob("*.tmp"))


def test_second_identical_call_is_a_cache_hit_with_no_api_call_and_no_cost(
    models: ModelsConfig, tmp_path: Path
) -> None:
    fake = FakeInstructor((Answer(value="a"), [(1000, 200)]))
    client, tracker, _ = build(models, tmp_path, fake)
    call(client)
    spent = tracker.run_total()
    again = call(client)
    assert again.value == "a" and len(fake.calls) == 1
    assert tracker.run_total() == spent and len(ledger(tmp_path)) == 1


def test_replay_mode_serves_a_recorded_entry_without_calling_or_spending(
    models: ModelsConfig, tmp_path: Path
) -> None:
    recorder, _, _ = build(models, tmp_path, FakeInstructor((Answer(value="a"), [(10, 5)])))
    call(recorder)
    before = ledger(tmp_path)
    fake = FakeInstructor()
    replayer, tracker, _ = build(models, tmp_path, fake, mode="replay", run_id="run-2")
    assert call(replayer).value == "a"
    assert fake.calls == [] and tracker.run_total() == 0 and ledger(tmp_path) == before


def test_replay_miss_raises_before_any_call(models: ModelsConfig, tmp_path: Path) -> None:
    fake = FakeInstructor()
    client, _, _ = build(models, tmp_path, fake, mode="replay")
    with pytest.raises(CacheMissError) as info:
        call(client)
    assert info.value.task == "header_map" and len(info.value.key) == 64
    assert fake.calls == [] and ledger(tmp_path) == []


def test_cache_key_includes_the_response_schema(models: ModelsConfig, tmp_path: Path) -> None:
    fake = FakeInstructor((Answer(value="a"), [(10, 5)]), (OtherAnswer(value="b"), [(10, 5)]))
    client, _, _ = build(models, tmp_path, fake)
    call(client, Answer)
    call(client, OtherAnswer)  # same prompt, different schema: must not hit the first entry
    assert len(fake.calls) == 2
    assert len(list((tmp_path / "cache").rglob("*.json"))) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"system": "other"},
        {"task": "coa_map"},
        {"model": "claude-sonnet-5-5"},
        {"user_content": [{"type": "text", "text": "x"}]},
    ],
)
def test_cache_key_includes_prompt_task_and_model(
    models: ModelsConfig, tmp_path: Path, change: dict[str, Any]
) -> None:
    fake = FakeInstructor((Answer(value="a"), [(10, 5)]), (Answer(value="b"), [(10, 5)]))
    client, _, _ = build(models, tmp_path, fake)
    call(client)
    call(client, **change)
    assert len(fake.calls) == 2


def test_off_mode_calls_live_and_writes_no_cache(models: ModelsConfig, tmp_path: Path) -> None:
    fake = FakeInstructor((Answer(value="a"), [(10, 5)]), (Answer(value="a"), [(10, 5)]))
    client, _, _ = build(models, tmp_path, fake, mode="off")
    call(client)
    call(client)
    assert len(fake.calls) == 2
    assert not (tmp_path / "cache").exists() or not list((tmp_path / "cache").rglob("*.json"))


# ---------- guards ----------


def test_budget_guard_raises_before_any_call(models: ModelsConfig, tmp_path: Path) -> None:
    fake = FakeInstructor((Answer(value="a"), [(10, 5)]))
    client, _, _ = build(models, tmp_path, fake, run_cap="0.0001")
    # worst case = est input + 1024 output tokens at $0.50/M is about $0.0005 > $0.0001
    with pytest.raises(BudgetExceededError, match="run cap"):
        call(client)
    assert fake.calls == [] and ledger(tmp_path) == []


def test_lifetime_cap_counts_the_existing_ledger(models: ModelsConfig, tmp_path: Path) -> None:
    (tmp_path / "ledger.jsonl").write_text(
        json.dumps({"cost_usd": "9.9999", "run_id": "old"}) + "\n", encoding="utf-8"
    )
    fake = FakeInstructor((Answer(value="a"), [(10, 5)]))
    client, _, _ = build(models, tmp_path, fake, total_cap="10.00")
    with pytest.raises(BudgetExceededError, match="lifetime"):
        call(client)
    assert fake.calls == []


def test_a_cache_hit_needs_no_budget(models: ModelsConfig, tmp_path: Path) -> None:
    recorder, _, _ = build(models, tmp_path, FakeInstructor((Answer(value="a"), [(10, 5)])))
    call(recorder)
    fake = FakeInstructor()
    broke, _, _ = build(models, tmp_path, fake, run_cap="0.0000001", run_id="run-2")
    assert call(broke).value == "a"


def test_size_guard_raises_before_any_call(models: ModelsConfig, tmp_path: Path) -> None:
    fake = FakeInstructor((Answer(value="a"), [(10, 5)]))
    client, _, _ = build(models, tmp_path, fake)
    huge = [{"type": "text", "text": "x" * (models.max_prompt_tokens * 3 + 10)}]
    with pytest.raises(PromptTooLargeError):
        call(client, user_content=huge)
    assert fake.calls == [] and ledger(tmp_path) == []


def test_images_count_1600_tokens_each_toward_the_size_guard(
    models: ModelsConfig, tmp_path: Path
) -> None:
    images = [{"type": "image", "source": {"type": "base64", "data": "AA=="}}] * 57
    assert estimate_prompt_tokens("", images, 3) == 57 * 1600 > models.max_prompt_tokens
    fake = FakeInstructor((Answer(value="a"), [(10, 5)]))
    client, _, _ = build(models, tmp_path, fake)
    with pytest.raises(PromptTooLargeError):
        call(client, user_content=images)


def test_estimate_prompt_tokens() -> None:
    assert estimate_prompt_tokens("abc", [{"type": "text", "text": "x" * 6}], 3) == 3
    assert (
        estimate_prompt_tokens("", [{"type": "image"}, {"type": "text", "text": "abcd"}], 3) == 1602
    )


def test_live_call_without_an_api_key_is_a_config_error(
    models: ModelsConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cache = LLMCache(tmp_path / "cache", mode="record", version=1)
    tracker = CostTracker(models, tmp_path / "ledger.jsonl", run_id="r")
    client = AnthropicLLMClient(models, cache, tracker)
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        call(client)


def test_replay_never_needs_a_key_or_the_sdk(
    models: ModelsConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cache = LLMCache(tmp_path / "cache", mode="replay", version=1)
    tracker = CostTracker(models, tmp_path / "ledger.jsonl", run_id="r")
    with pytest.raises(CacheMissError):
        call(AnthropicLLMClient(models, cache, tracker))


def test_importing_the_client_does_not_import_the_sdk() -> None:
    code = (
        "import sys, cre_screen.mapping.llm_client; "
        "assert 'anthropic' not in sys.modules and 'instructor' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
