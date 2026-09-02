"""
Metrics for TTT experiments.

The metrics preserve vector costs and report exactly the quantities
pre-registered in the proposal: action density, wasted-call rate,
acceptance alpha, gamma throughput and oracle regret.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.errors import ValidationError
from ..core.types import Cost


@dataclass(frozen=True, slots=True)
class DensityMetrics:
    useful_actions: int
    total_actions: int
    env_calls: int
    accepted: int
    drafted: int
    wall_ms: float
    oracle_cost: Cost | None = None
    observed_cost: Cost | None = None

    @property
    def action_density(self) -> float:
        return self.useful_actions / self.total_actions if self.total_actions else 0.0

    @property
    def wasted_call_rate(self) -> float:
        if self.env_calls <= 0:
            return 0.0
        wasted = max(0, self.env_calls - self.useful_actions)
        return wasted / self.env_calls

    @property
    def acceptance_alpha(self) -> float:
        return self.accepted / self.drafted if self.drafted else 0.0

    @property
    def throughput_actions_per_ms(self) -> float:
        return self.useful_actions / self.wall_ms if self.wall_ms > 0 else 0.0

    @property
    def regret_env_calls(self) -> int | None:
        if self.oracle_cost is None or self.observed_cost is None:
            return None
        return max(0, self.observed_cost.env_calls - self.oracle_cost.env_calls)


def gamma_throughput(alpha: float, gamma: int, verify_ms: float, draft_exec_ms: float) -> float:
    """Expected committed actions per millisecond under fixed alpha/gamma."""
    if gamma < 0:
        raise ValidationError("gamma must be non-negative")
    if not 0.0 <= alpha <= 1.0:
        raise ValidationError("alpha must be in [0, 1]")
    if alpha == 1.0:
        expected = gamma + 1.0
    else:
        expected = (1.0 - alpha ** (gamma + 1)) / (1.0 - alpha)
    latency = verify_ms + gamma * draft_exec_ms
    return expected / latency if latency > 0 else 0.0


__all__ = ["DensityMetrics", "gamma_throughput"]
