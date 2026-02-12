"""Cost tracking and budget management for multi-agent tasks."""

import logging
from typing import Callable

from schemas import CostSnapshot, TaskCostSummary

logger = logging.getLogger(__name__)

# Pricing per 1M tokens (input, output)
MODEL_RATES = {
    "opus": (15.0, 75.0),
    "sonnet": (3.0, 15.0),
    "haiku": (0.25, 1.25),
}

BUDGET_THRESHOLDS = [50, 80, 100]


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate cost in USD for a model invocation."""
    input_rate, output_rate = MODEL_RATES.get(model, MODEL_RATES["sonnet"])
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000


class CostTracker:
    """Tracks costs across agents for a task."""

    def __init__(self, task_id: str, budget_usd: float = 15.0,
                 on_threshold: Callable[[str, int, float, float], None] | None = None):
        """
        Args:
            task_id: The task being tracked.
            budget_usd: Total budget for the task.
            on_threshold: Callback(task_id, percent, current_cost, budget)
                          called when a threshold is crossed.
        """
        self.task_id = task_id
        self.budget_usd = budget_usd
        self.snapshots: list[CostSnapshot] = []
        self._on_threshold = on_threshold
        self._thresholds_fired: set[int] = set()

    @property
    def total_cost(self) -> float:
        return sum(s.cost_usd for s in self.snapshots)

    @property
    def total_tokens(self) -> int:
        return sum(s.input_tokens + s.output_tokens for s in self.snapshots)

    @property
    def budget_percent(self) -> float:
        if self.budget_usd <= 0:
            return 0.0
        return (self.total_cost / self.budget_usd) * 100

    @property
    def budget_exceeded(self) -> bool:
        return self.total_cost >= self.budget_usd

    def record(self, agent_role: str, model: str,
               input_tokens: int, output_tokens: int,
               duration_seconds: float = 0.0) -> CostSnapshot:
        """Record an agent's cost and check thresholds."""
        cost = calculate_cost(model, input_tokens, output_tokens)
        snapshot = CostSnapshot(
            agent_role=agent_role,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            duration_seconds=duration_seconds,
        )
        self.snapshots.append(snapshot)

        # Check thresholds
        self._check_thresholds()

        return snapshot

    def _check_thresholds(self):
        pct = self.budget_percent
        for threshold in BUDGET_THRESHOLDS:
            if pct >= threshold and threshold not in self._thresholds_fired:
                self._thresholds_fired.add(threshold)
                logger.info(
                    "Task %s: budget %d%% ($%.2f/$%.2f)",
                    self.task_id, threshold, self.total_cost, self.budget_usd,
                )
                if self._on_threshold:
                    self._on_threshold(
                        self.task_id, threshold, self.total_cost, self.budget_usd,
                    )

    def summary(self) -> TaskCostSummary:
        """Build a cost summary for the task."""
        return TaskCostSummary(
            task_id=self.task_id,
            agents=list(self.snapshots),
            total_cost_usd=self.total_cost,
            total_tokens=self.total_tokens,
            total_duration_seconds=sum(s.duration_seconds for s in self.snapshots),
            budget_usd=self.budget_usd,
        )

    def format_dashboard_line(self) -> str:
        """Format a one-line cost summary for the Telegram dashboard."""
        return f"Budget: ${self.total_cost:.2f}/${self.budget_usd:.2f} ({self.budget_percent:.0f}%)"
