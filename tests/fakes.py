"""Test doubles. Tests never call the real API; they use FakeLLMClient."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from cre_screen.errors import CacheMissError

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class FakeCall:
    task: str
    response_model: type[BaseModel]
    system: str
    user_content: list[dict[str, Any]]
    model: str | None


def _request_text(system: str, user_content: list[dict[str, Any]]) -> str:
    parts = [system]
    parts.extend(str(block["text"]) for block in user_content if block.get("type") == "text")
    return "\n".join(parts)


class FakeLLMClient:
    """Implements the ``LLMClient.structured`` protocol from a canned-response dict.

    ``responses`` maps ``(task, frozenset_of_key_inputs)`` to a response object. A request
    matches an entry when the task is equal and every string in the frozenset appears in the
    request text (system prompt plus all text blocks). When several entries match, the one
    with the most key inputs wins, so a specific entry beats a generic one.

    Every call is appended to ``calls`` before matching, so tests can assert the LLM was not
    called (``fake.calls == []``) on the synonym-table path.
    """

    def __init__(self, responses: dict[tuple[str, frozenset[str]], BaseModel] | None = None):
        self.responses: dict[tuple[str, frozenset[str]], BaseModel] = dict(responses or {})
        self.calls: list[FakeCall] = []

    def calls_for(self, task: str) -> list[FakeCall]:
        return [c for c in self.calls if c.task == task]

    def structured(
        self,
        *,
        task: str,
        response_model: type[T],
        system: str,
        user_content: list[dict[str, Any]],
        model: str | None = None,
    ) -> T:
        self.calls.append(FakeCall(task, response_model, system, user_content, model))
        text = _request_text(system, user_content)
        best: BaseModel | None = None
        best_size = -1
        for (entry_task, key_inputs), response in self.responses.items():
            if entry_task != task:
                continue
            if all(item in text for item in key_inputs) and len(key_inputs) > best_size:
                best, best_size = response, len(key_inputs)
        if best is None:
            raise CacheMissError(task, "fake-llm-no-canned-response")
        if not isinstance(best, response_model):
            raise TypeError(
                f"canned response for {task} is {type(best).__name__}, "
                f"expected {response_model.__name__}"
            )
        return best
