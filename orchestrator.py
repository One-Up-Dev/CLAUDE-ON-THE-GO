"""Multi-agent orchestrator — FSM-based sequential execution with test gates."""

import asyncio
import json
import logging
import os
import time

from agent_runner import AgentRunner
from config import Config
from cost_tracker import CostTracker
from db import create_task, get_task, update_task
from regression import RegressionTracker
from schemas import (
    AgentConfig,
    AgentDashboardEntry,
    AgentStatus,
    AgentTask,
    ExecutionPlan,
    TaskDashboard,
    TaskStatus,
    TestLevel,
)
from test_gate import capture_baseline, format_compact, run_test_level
from worktree import WorktreeManager

logger = logging.getLogger(__name__)

# Default agent configs for MVP (Phase 1 — sequential)
AGENT_DEFAULTS = {
    "planner": AgentConfig(
        role="planner",
        prompt_file=os.path.expanduser("~/.claude/agents/cotg-planner.md"),
        model="opus",
        timeout=120,
        budget_usd=3.0,
    ),
    "rust-backend": AgentConfig(
        role="rust-backend",
        prompt_file=os.path.expanduser("~/.claude/agents/cotg-rust-backend.md"),
        model="sonnet",
        timeout=600,
        budget_usd=1.50,
    ),
    "rust-frontend": AgentConfig(
        role="rust-frontend",
        prompt_file=os.path.expanduser("~/.claude/agents/cotg-rust-frontend.md"),
        model="sonnet",
        timeout=600,
        budget_usd=1.50,
    ),
    "rust-database": AgentConfig(
        role="rust-database",
        prompt_file=os.path.expanduser("~/.claude/agents/cotg-rust-database.md"),
        model="sonnet",
        timeout=300,
        budget_usd=1.50,
    ),
    "rust-architect": AgentConfig(
        role="rust-architect",
        prompt_file=os.path.expanduser("~/.claude/agents/cotg-rust-architect.md"),
        model="sonnet",
        timeout=300,
        budget_usd=2.0,
    ),
    "tester-cargo": AgentConfig(
        role="tester-cargo",
        prompt_file=os.path.expanduser("~/.claude/agents/cotg-tester-cargo.md"),
        model="sonnet",
        timeout=600,
        budget_usd=1.0,
    ),
}


class Orchestrator:
    """Orchestrates multi-agent Rust builds with test gates."""

    def __init__(self, config: Config, project_path: str, description: str,
                 on_progress=None, on_budget_alert=None):
        """
        Args:
            config: Bot config.
            project_path: Path to the Rust project.
            description: Task description from user.
            on_progress: async callback(dashboard: TaskDashboard) for Telegram updates.
            on_budget_alert: async callback(task_id, percent, cost, budget).
        """
        self.config = config
        self.project_path = project_path
        self.description = description
        self._on_progress = on_progress
        self._on_budget_alert = on_budget_alert

        self.task_id: str | None = None
        self.status = TaskStatus.PENDING
        self.plan: ExecutionPlan | None = None
        self.worktrees: WorktreeManager | None = None
        self.cost_tracker: CostTracker | None = None
        self.regression_tracker: RegressionTracker | None = None
        self.dashboard_entries: dict[str, AgentDashboardEntry] = {}
        self._handoff_lines: list[str] = []

    async def execute(self) -> TaskDashboard:
        """Run the full orchestration pipeline. Returns final dashboard."""
        t0 = time.monotonic()

        # 1. Create task in DB
        self.task_id = create_task(self.project_path, self.description)
        self.cost_tracker = CostTracker(
            self.task_id,
            budget_usd=self.config.build_budget_usd,
            on_threshold=self._handle_budget_threshold,
        )

        try:
            # 2. Capture test baseline
            await self._set_status(TaskStatus.PLANNING)
            baseline = await capture_baseline(self.project_path)
            self.regression_tracker = RegressionTracker(self.task_id, baseline)
            logger.info("Baseline: %d tests, %d passing", baseline.total_tests, baseline.passing_tests)

            # 3. Run planner
            plan = await self._run_planner()
            self.plan = plan
            update_task(self.task_id, plan_json=plan.model_dump_json())

            # 4. Create worktrees
            self.worktrees = WorktreeManager(self.project_path, self.task_id)

            # 5. Execute agents sequentially with test gates
            await self._set_status(TaskStatus.EXECUTING)
            for agent_task in plan.agents:
                if self.cost_tracker.budget_exceeded:
                    logger.warning("Budget exceeded, stopping execution")
                    break
                await self._execute_agent(agent_task)

            # 6. Merge
            await self._set_status(TaskStatus.MERGING)
            integration_branch = f"cotg/integration/{self.task_id}"
            conflicts = await self.worktrees.merge_to_integration(integration_branch)
            if conflicts:
                raise RuntimeError(f"Merge conflicts: {'; '.join(conflicts)}")
            update_task(self.task_id, integration_branch=integration_branch)

            # 7. Level 2 tests on integration
            await self._set_status(TaskStatus.TESTING)
            level2 = await run_test_level(TestLevel.NORMAL, self.project_path)
            if not level2.passed:
                raise RuntimeError(f"Level 2 tests failed after merge: {format_compact(level2)}")

            # 8. Done
            await self._set_status(TaskStatus.DONE)
            duration = time.monotonic() - t0
            update_task(
                self.task_id,
                status="done",
                total_cost_usd=self.cost_tracker.total_cost,
                total_tokens=self.cost_tracker.total_tokens,
                completed_at=__import__("db")._now_iso(),
            )
            logger.info(
                "Task %s completed in %.1fs, cost $%.2f",
                self.task_id, duration, self.cost_tracker.total_cost,
            )

        except Exception as e:
            logger.error("Task %s failed: %s", self.task_id, e)
            await self._set_status(TaskStatus.ERROR)
            update_task(self.task_id, status="error", error=str(e)[:500])

        finally:
            # Cleanup worktrees
            if self.worktrees:
                await self.worktrees.cleanup()

        return self._build_dashboard()

    async def _run_planner(self) -> ExecutionPlan:
        """Run the planner agent to produce an execution plan."""
        planner_config = AGENT_DEFAULTS["planner"]
        runner = AgentRunner(self.config, planner_config, self.task_id)

        prompt = (
            f"Analyze the Rust project at {self.project_path} and plan the following task:\n\n"
            f"{self.description}\n\n"
            "Read Cargo.toml, understand the project structure, and produce a JSON execution plan."
        )

        result = await runner.run(prompt, cwd=self.project_path)

        # Record cost
        self.cost_tracker.record(
            "planner", planner_config.model,
            result.input_tokens, result.output_tokens,
            result.duration_seconds,
        )

        # Parse plan from output
        try:
            # Try to extract JSON from the output
            plan_json = _extract_json(result.raw_output)
            plan = ExecutionPlan(task_id=self.task_id, description=self.description, **plan_json)
        except Exception as e:
            logger.warning("Failed to parse planner output as ExecutionPlan: %s", e)
            # Fallback: single backend agent
            plan = ExecutionPlan(
                task_id=self.task_id,
                description=self.description,
                agents=[
                    AgentTask(
                        role="rust-backend",
                        description=self.description,
                    )
                ],
            )

        self._handoff_lines.append(f"## Plan\n{self.description}")
        return plan

    async def _execute_agent(self, agent_task: AgentTask) -> None:
        """Execute a single agent with test gate and retry logic."""
        role = agent_task.role
        agent_config = AGENT_DEFAULTS.get(role)
        if not agent_config:
            logger.warning("Unknown agent role: %s, skipping", role)
            return

        # Dashboard entry
        self.dashboard_entries[role] = AgentDashboardEntry(role=role, status="running")
        await self._notify_progress()

        # Create worktree
        wt_path = await self.worktrees.create(role)

        try:
            error_context = ""
            for attempt in range(1, self.config.build_max_retries + 1):
                runner = AgentRunner(self.config, agent_config, self.task_id)

                prompt = (
                    f"Task: {agent_task.description}\n\n"
                    f"Files to modify: {', '.join(agent_task.files_to_modify) or 'as needed'}\n"
                    f"Files to create: {', '.join(agent_task.files_to_create) or 'as needed'}"
                )

                result = await runner.run(
                    prompt, cwd=wt_path,
                    handoff_context="\n".join(self._handoff_lines),
                    error_context=error_context,
                )

                # Record cost
                self.cost_tracker.record(
                    role, agent_config.model,
                    result.input_tokens, result.output_tokens,
                    result.duration_seconds,
                )

                # Commit agent work
                commit_msg = f"feat({role}): {agent_task.description[:60]}"
                await self.worktrees.commit_agent_work(role, commit_msg)

                # Level 1 test gate
                level1 = await run_test_level(TestLevel.FAST, wt_path)

                if result.status == AgentStatus.SUCCESS and level1.passed:
                    # Success — update dashboard and move on
                    self.dashboard_entries[role] = AgentDashboardEntry(
                        role=role, status="done",
                        cost_usd=self.cost_tracker.total_cost,
                        duration_seconds=result.duration_seconds,
                        tokens=result.input_tokens + result.output_tokens,
                    )
                    self._handoff_lines.append(
                        f"## {role} (done)\n"
                        f"Files: {', '.join(result.files_modified)}\n"
                        f"Tests added: {result.tests_added}"
                    )
                    await self._notify_progress()
                    return

                # Failed — prepare retry context
                error_context = format_compact(level1)
                if result.errors:
                    error_context += "\n" + "\n".join(result.errors)

                logger.warning(
                    "Agent %s attempt %d failed: %s",
                    role, attempt, error_context[:200],
                )

                # Check regression
                if self.regression_tracker:
                    delta = self.regression_tracker.check(role, level1)
                    if delta.newly_failing > 0:
                        error_context += f"\nREGRESSION: {delta.newly_failing} tests broke"

            # All retries exhausted
            self.dashboard_entries[role] = AgentDashboardEntry(role=role, status="error")
            await self._notify_progress()
            raise RuntimeError(f"Agent {role} failed after {self.config.build_max_retries} retries")

        except Exception:
            # Cleanup this agent's worktree immediately on failure
            await self.worktrees.remove(role)
            raise

    async def _set_status(self, status: TaskStatus):
        self.status = status
        if self.task_id:
            update_task(self.task_id, status=status.value)
        await self._notify_progress()

    async def _notify_progress(self):
        if self._on_progress:
            try:
                await self._on_progress(self._build_dashboard())
            except Exception as e:
                logger.debug("Progress callback error: %s", e)

    def _handle_budget_threshold(self, task_id, percent, cost, budget):
        if self._on_budget_alert:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    self._on_budget_alert(task_id, percent, cost, budget)
                )
            except RuntimeError:
                pass

    def _build_dashboard(self) -> TaskDashboard:
        baseline_tests = self.regression_tracker.baseline.total_tests if self.regression_tracker else 0
        regressions = self.regression_tracker.total_regressions() if self.regression_tracker else 0
        return TaskDashboard(
            task_id=self.task_id or "",
            description=self.description,
            status=self.status,
            agents=list(self.dashboard_entries.values()),
            total_cost_usd=self.cost_tracker.total_cost if self.cost_tracker else 0,
            budget_usd=self.cost_tracker.budget_usd if self.cost_tracker else self.config.build_budget_usd,
            baseline_tests=baseline_tests,
            regressions=regressions,
        )


def _extract_json(text: str) -> dict:
    """Extract the first JSON object from text."""
    # Try to find JSON between ```json ... ```
    import re
    match = re.search(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    # Try to find raw JSON object
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError("No JSON found in output")


def format_dashboard(dashboard: TaskDashboard) -> str:
    """Format dashboard for Telegram display."""
    status_icons = {
        "waiting": "\u23f8",  # pause
        "running": "\u23f3",  # hourglass
        "done": "\u2713",     # check
        "error": "\u2717",    # cross
    }

    lines = [
        f"Build: {dashboard.description[:40]}",
        f"Status: {dashboard.status.value} | Budget: ${dashboard.total_cost_usd:.2f}/${dashboard.budget_usd:.2f}",
        "",
    ]

    for agent in dashboard.agents:
        icon = status_icons.get(agent.status, "?")
        cost_str = f"${agent.cost_usd:.2f}" if agent.cost_usd > 0 else ""
        dur_str = f"{agent.duration_seconds:.0f}s" if agent.duration_seconds > 0 else ""
        tok_str = f"{agent.tokens // 1000}k tok" if agent.tokens > 0 else ""
        parts = [p for p in [cost_str, dur_str, tok_str] if p]
        detail = "  ".join(parts)
        lines.append(f"{icon} {agent.role}  {detail}")

    if dashboard.baseline_tests > 0:
        lines.append("")
        lines.append(f"Tests: {dashboard.baseline_tests} baseline, {dashboard.regressions} regressions")

    return "\n".join(lines)
