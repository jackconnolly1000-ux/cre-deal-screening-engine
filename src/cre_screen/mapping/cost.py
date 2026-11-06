"""Cost math, spend caps, and the append-only ``.cost_ledger.jsonl``."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from ..config import ModelsConfig
from ..errors import BudgetExceededError, ConfigError

logger = logging.getLogger(__name__)

_MILLION = Decimal(1_000_000)


class CostTracker:
    """Prices calls, enforces the per-run and lifetime caps, and writes the ledger.

    Costs are kept at full Decimal precision: a single Haiku call costs a fraction of a cent,
    so quantizing each ledger line to cents would record most calls as $0.00.
    """

    def __init__(self, cfg: ModelsConfig, ledger_path: Path, *, run_id: str) -> None:
        self._cfg = cfg
        self.ledger_path = ledger_path
        self.run_id = run_id
        self._run_total = Decimal("0")

    def cost_usd(self, model: str, input_tokens: int, output_tokens: int) -> Decimal:
        price = self._cfg.pricing_usd_per_mtok.get(model)
        if price is None:
            raise ConfigError(f"no pricing for model {model!r} in models.yaml")
        in_price, out_price = price.input, price.output
        threshold = price.long_context_threshold
        if threshold is not None and input_tokens > threshold:
            if price.input_long is None or price.output_long is None:
                raise ConfigError(f"{model}: long_context_threshold set without long prices")
            in_price, out_price = price.input_long, price.output_long
        return Decimal(input_tokens) * in_price / _MILLION + Decimal(output_tokens) * out_price / (
            _MILLION
        )

    def worst_case_cost(self, model: str, est_input_tokens: int, max_tokens: int) -> Decimal:
        """Cost if the call uses the estimated input and runs to max_tokens of output."""
        return self.cost_usd(model, est_input_tokens, max_tokens)

    def run_total(self) -> Decimal:
        return self._run_total

    def lifetime_total(self) -> Decimal:
        """Sum of every ledger line (all runs, including this one)."""
        if not self.ledger_path.exists():
            return Decimal("0")
        total = Decimal("0")
        with self.ledger_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    total += Decimal(json.loads(stripped)["cost_usd"])
        return total

    def check_can_spend(self, worst_case: Decimal) -> None:
        budget = self._cfg.budget
        if self._run_total + worst_case > budget.per_run_cap_usd:
            raise BudgetExceededError(
                f"run cap ${budget.per_run_cap_usd}: spent ${self._run_total} "
                f"+ worst case ${worst_case}"
            )
        lifetime = self.lifetime_total()
        if lifetime + worst_case > budget.total_cap_usd:
            raise BudgetExceededError(
                f"lifetime cap ${budget.total_cap_usd}: spent ${lifetime} "
                f"+ worst case ${worst_case}"
            )

    def record(
        self,
        *,
        task: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        attempt: int = 1,
    ) -> Decimal:
        """Append one ledger line for one API attempt and return its cost."""
        cost = self.cost_usd(model, input_tokens, output_tokens)
        line = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "task": task,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": str(cost),
            "attempt": attempt,
        }
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(line, sort_keys=False) + "\n")
        self._run_total += cost
        logger.info("recorded %s attempt %d: $%s", task, attempt, cost)
        return cost
