"""Regression tracking — detect and log test regressions per agent."""

import logging

from db import log_regression
from schemas import TestBaseline, TestDelta, TestResult
from test_gate import compare_to_baseline

logger = logging.getLogger(__name__)


class RegressionTracker:
    """Tracks per-agent test deltas and regression rates."""

    def __init__(self, task_id: str, baseline: TestBaseline):
        self.task_id = task_id
        self.baseline = baseline
        self.per_agent: dict[str, TestDelta] = {}

    def check(self, agent_role: str, result: TestResult) -> TestDelta:
        """Compare test result to baseline, log if regression found."""
        delta = compare_to_baseline(self.baseline, result)
        self.per_agent[agent_role] = delta

        # Log to DB
        log_regression(
            task_id=self.task_id,
            agent_role=agent_role,
            tests_before=delta.total_before,
            tests_after=delta.total_after,
            regressions=delta.newly_failing,
            new_tests=delta.newly_added,
        )

        if delta.newly_failing > 0:
            logger.warning(
                "REGRESSION: %s broke %d tests (baseline: %d passing → %d passing)",
                agent_role, delta.newly_failing,
                delta.passing_before, delta.passing_after,
            )

        return delta

    def has_regression(self, agent_role: str) -> bool:
        """Check if a specific agent caused regressions."""
        delta = self.per_agent.get(agent_role)
        return delta is not None and delta.newly_failing > 0

    def regression_rate(self, agent_role: str) -> float:
        """Ratio of tests broken by this agent."""
        delta = self.per_agent.get(agent_role)
        if not delta or self.baseline.passing_tests == 0:
            return 0.0
        return delta.newly_failing / self.baseline.passing_tests

    def total_regressions(self) -> int:
        """Total regressions across all agents."""
        return sum(d.newly_failing for d in self.per_agent.values())

    def summary(self) -> str:
        """Human-readable regression summary."""
        lines = []
        for role, delta in self.per_agent.items():
            status = "OK" if delta.newly_failing == 0 else "REGRESSION"
            lines.append(
                f"{role}: {status} — "
                f"{delta.passing_after}/{delta.total_after} passing "
                f"(+{delta.newly_added} new, -{delta.newly_failing} broken)"
            )
        return "\n".join(lines)
