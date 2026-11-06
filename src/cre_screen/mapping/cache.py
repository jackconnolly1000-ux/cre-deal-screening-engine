"""On-disk replay cache for structured LLM responses (``evals/llm_cache``)."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel

from ..errors import CacheMissError, ConfigError

logger = logging.getLogger(__name__)

CacheMode = Literal["replay", "record", "off"]

_M = TypeVar("_M", bound=BaseModel)


class LLMCache:
    def __init__(self, directory: Path, *, mode: CacheMode, version: int) -> None:
        self.directory = directory
        self.mode = mode
        self.version = version

    def make_key(
        self,
        *,
        model: str,
        task: str,
        system: str,
        user_content: list[dict[str, Any]],
        response_model: type[BaseModel],
        temperature: float,
        max_tokens: int,
    ) -> str:
        payload = {
            "v": self.version,
            "model": model,
            "task": task,
            "system": system,
            "user_content": user_content,
            "schema": response_model.model_json_schema(),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key: str, response_model: type[_M]) -> _M | None:
        """Return the validated cached response, or None on a miss (always None when off)."""
        if self.mode == "off":
            return None
        path = self._path(key)
        if not path.exists():
            return None
        entry = json.loads(path.read_text(encoding="utf-8"))
        return response_model.model_validate(entry["response"])

    def require(self, key: str, *, task: str, response_model: type[_M]) -> _M:
        """Like ``get`` but raises CacheMissError on a miss."""
        hit = self.get(key, response_model)
        if hit is None:
            raise CacheMissError(task, key)
        return hit

    def put(
        self,
        *,
        key: str,
        task: str,
        model: str,
        response: BaseModel,
        input_tokens: int,
        output_tokens: int,
        attempts: int,
        cost_usd: str,
    ) -> Path:
        """Write an entry atomically (temp file, then replace). Record mode only."""
        if self.mode != "record":
            raise ConfigError(f"cache writes need mode 'record' (mode is {self.mode!r})")
        entry = {
            "key": key,
            "task": task,
            "model": model,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "response": response.model_dump(mode="json"),
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "attempts": attempts,
            },
            "cost_usd": cost_usd,
        }
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
        logger.info("cached %s response at %s", task, path.as_posix())
        return path
