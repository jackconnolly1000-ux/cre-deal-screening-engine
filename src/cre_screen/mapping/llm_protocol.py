"""The LLM client interface, importable without ``anthropic`` or ``instructor``.

``mapping/llm_client.py`` (M3) implements it. Tests use ``tests/fakes.py::FakeLLMClient``.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel

from cre_screen.errors import LLMUnavailableError

TaskName = Literal["header_map", "coa_map", "concession", "vision_page"]
T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    def structured(
        self,
        *,
        task: TaskName,
        response_model: type[T],
        system: str,
        user_content: list[dict[str, Any]],
        model: str | None = None,
    ) -> T: ...


class NullLLMClient:
    """Stands in where no live client exists yet; any call is an error with a clear message."""

    def structured(
        self,
        *,
        task: TaskName,
        response_model: type[T],
        system: str,
        user_content: list[dict[str, Any]],
        model: str | None = None,
    ) -> T:
        raise LLMUnavailableError(
            f"step '{task}' needs the LLM, which is not available in this command yet"
        )
