from __future__ import annotations

import threading
from dataclasses import dataclass
from decimal import Decimal

from integrations.errors import BudgetExceeded


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    max_requests: int
    max_cost_usd: Decimal

    def __post_init__(self) -> None:
        if self.max_requests < 0 or self.max_cost_usd < 0:
            raise ValueError("Budget limits cannot be negative")


@dataclass(frozen=True, slots=True)
class ModelPricing:
    input_usd_per_million_tokens: Decimal
    output_usd_per_million_tokens: Decimal

    def __post_init__(self) -> None:
        if self.input_usd_per_million_tokens < 0 or self.output_usd_per_million_tokens < 0:
            raise ValueError("Model pricing cannot be negative")

    def estimate(self, input_tokens: int, output_tokens: int) -> Decimal:
        million = Decimal(1_000_000)
        return (
            Decimal(input_tokens) * self.input_usd_per_million_tokens / million
            + Decimal(output_tokens) * self.output_usd_per_million_tokens / million
        )


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    requests_used: int
    cost_committed_usd: Decimal
    max_requests: int
    max_cost_usd: Decimal


class BudgetGuard:
    """Thread-safe, conservative pre-request budget accounting.

    Estimated cost is committed before transport execution and is not refunded
    after a transport error because a remote provider may already have billed it.
    """

    def __init__(self, limits: BudgetLimits):
        self._limits = limits
        self._requests_used = 0
        self._cost_committed = Decimal("0")
        self._lock = threading.Lock()

    def consume(self, estimated_cost_usd: Decimal) -> BudgetSnapshot:
        if estimated_cost_usd < 0:
            raise ValueError("Estimated cost cannot be negative")
        with self._lock:
            if self._requests_used + 1 > self._limits.max_requests:
                raise BudgetExceeded("AI request-count budget exhausted")
            if self._cost_committed + estimated_cost_usd > self._limits.max_cost_usd:
                raise BudgetExceeded("AI cost budget would be exceeded")
            self._requests_used += 1
            self._cost_committed += estimated_cost_usd
            return self._snapshot_unlocked()

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            requests_used=self._requests_used,
            cost_committed_usd=self._cost_committed,
            max_requests=self._limits.max_requests,
            max_cost_usd=self._limits.max_cost_usd,
        )
