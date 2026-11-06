"""The Anthropic-backed LLM client. The only module that imports ``anthropic`` or ``instructor``.

Call sequence for ``structured`` (SPEC 7.7): cache key, cache hit, replay-mode miss, size guard,
budget guard, Instructor call (every attempt's usage recorded through the
``completion:response`` hook), ledger lines, atomic cache write in ``record`` mode.
"""

from __future__ import annotations

import logging
import math
import os
from decimal import Decimal
from typing import Any, TypeVar

from pydantic import BaseModel

from cre_screen.config import ModelsConfig
from cre_screen.errors import CacheMissError, ConfigError, PromptTooLargeError
from cre_screen.mapping.cache import LLMCache
from cre_screen.mapping.cost import CostTracker
from cre_screen.mapping.llm_protocol import LLMClient, TaskName

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

IMAGE_TOKEN_ESTIMATE = 1_600
API_KEY_ENV = "ANTHROPIC_API_KEY"

__all__ = ["AnthropicLLMClient", "LLMClient", "TaskName", "estimate_prompt_tokens"]


def estimate_prompt_tokens(
    system: str, user_content: list[dict[str, Any]], chars_per_token: int
) -> int:
    """Conservative pre-call estimate: characters / chars_per_token, images count 1,600 each."""
    chars = len(system)
    images = 0
    for block in user_content:
        if block.get("type") == "image":
            images += 1
        else:
            chars += len(str(block.get("text", "")))
    return math.ceil(chars / chars_per_token) + images * IMAGE_TOKEN_ESTIMATE


class _AttemptState:
    def __init__(self, task: str, model: str) -> None:
        self.task = task
        self.model = model
        self.attempts = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost = Decimal("0")


class AnthropicLLMClient:
    def __init__(
        self,
        cfg: ModelsConfig,
        cache: LLMCache,
        tracker: CostTracker,
        *,
        instructor_client: Any | None = None,
    ) -> None:
        self._cfg = cfg
        self._cache = cache
        self._tracker = tracker
        self._instructor = instructor_client
        self._active: _AttemptState | None = None
        if instructor_client is not None:
            instructor_client.on("completion:response", self._on_response)

    # ----- live client -----

    def _client(self) -> Any:
        if self._instructor is None:
            if not os.environ.get(API_KEY_ENV):
                raise ConfigError(f"{API_KEY_ENV} is not set; a live call needs it")
            import anthropic
            import instructor

            self._instructor = instructor.from_anthropic(anthropic.Anthropic())
            self._instructor.on("completion:response", self._on_response)
        return self._instructor

    def _on_response(self, response: Any) -> None:
        """Called by Instructor once per attempt, including attempts that later fail validation."""
        state = self._active
        if state is None:
            return
        usage = getattr(response, "usage", None)
        in_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        out_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        if usage is None:
            logger.warning(
                "attempt %d returned no usage; recording zero tokens", state.attempts + 1
            )
        state.attempts += 1
        state.input_tokens += in_tokens
        state.output_tokens += out_tokens
        state.cost += self._tracker.record(
            task=state.task,
            model=state.model,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            attempt=state.attempts,
        )

    # ----- protocol -----

    def structured(
        self,
        *,
        task: TaskName,
        response_model: type[T],
        system: str,
        user_content: list[dict[str, Any]],
        model: str | None = None,
    ) -> T:
        cfg = self._cfg
        model_id = model or cfg.default_model
        max_tokens: int = getattr(cfg.max_tokens, task)
        key = self._cache.make_key(
            model=model_id,
            task=task,
            system=system,
            user_content=user_content,
            response_model=response_model,
            temperature=cfg.temperature,
            max_tokens=max_tokens,
        )

        hit = self._cache.get(key, response_model)
        if hit is not None:
            return hit
        if self._cache.mode == "replay":
            raise CacheMissError(task, key)

        estimate = estimate_prompt_tokens(system, user_content, cfg.chars_per_token_estimate)
        if estimate > cfg.max_prompt_tokens:
            raise PromptTooLargeError(
                f"{task}: estimated {estimate} prompt tokens exceeds {cfg.max_prompt_tokens}"
            )
        self._tracker.check_can_spend(self._tracker.worst_case_cost(model_id, estimate, max_tokens))

        client = self._client()
        state = _AttemptState(task, model_id)
        self._active = state
        try:
            result: T = client.messages.create(
                model=model_id,
                max_tokens=max_tokens,
                temperature=cfg.temperature,
                system=system,
                messages=[{"role": "user", "content": user_content}],
                response_model=response_model,
                max_retries=cfg.max_retries,
            )
        finally:
            self._active = None

        if self._cache.mode == "record":
            self._cache.put(
                key=key,
                task=task,
                model=model_id,
                response=result,
                input_tokens=state.input_tokens,
                output_tokens=state.output_tokens,
                attempts=state.attempts,
                cost_usd=str(state.cost),
            )
        return result
