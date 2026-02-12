"""Pydantic models for the COTG multi-agent orchestration system."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# --- Enums ---


class TaskStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    EXECUTING = "executing"
    TESTING = "testing"
    MERGING = "merging"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class AgentStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    RETRYING = "retrying"


class TestLevel(int, Enum):
    SMOKE = 0       # cargo check --workspace
    FAST = 1        # cargo test --lib (<10s)
    NORMAL = 2      # cargo test --workspace (<60s)
    FULL = 3        # cargo test + trunk build + playwright (<5min)


# --- Rust Stack Detection ---


class RustStack(BaseModel):
    """Detected Rust ecosystem from Cargo.toml."""
    backend: str = "axum"           # axum, actix-web, rocket, warp
    frontend: str | None = None     # leptos, yew, dioxus, None
    database: str | None = None     # sqlx, sea-orm, diesel, None
    build_wasm: str | None = None   # cargo-leptos, trunk, wasm-pack, None
    extra_crates: list[str] = Field(default_factory=list)


# --- Test Baseline ---


class TestBaseline(BaseModel):
    """Snapshot of test state before a task begins."""
    total_tests: int = 0
    passing_tests: int = 0
    snapshot_hash: str = ""         # hash of `cargo test --list` output


class TestDelta(BaseModel):
    """Change in test results after an agent runs."""
    total_before: int = 0
    total_after: int = 0
    passing_before: int = 0
    passing_after: int = 0
    newly_failing: int = 0
    newly_added: int = 0


class TestResult(BaseModel):
    """Result of running a test level."""
    level: TestLevel
    passed: bool
    total_tests: int = 0
    passed_tests: int = 0
    failed_tests: list[str] = Field(default_factory=list)
    compiler_errors: list[str] = Field(default_factory=list)
    output: str = ""
    regressions: int = 0
    duration_seconds: float = 0.0


# --- Agent Configuration ---


class AgentConfig(BaseModel):
    """Configuration for a specialized agent role."""
    role: str                       # e.g. "rust-backend"
    prompt_file: str                # path to agent .md definition
    model: str = "sonnet"           # opus or sonnet
    timeout: int = 600              # seconds
    budget_usd: float = 1.50
    owned_files: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    read_only: bool = False


# --- Agent Result ---


class AgentResult(BaseModel):
    """Structured output from an agent run."""
    status: AgentStatus = AgentStatus.SUCCESS
    files_modified: list[str] = Field(default_factory=list)
    tests_added: int = 0
    errors: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    raw_output: str = ""


# --- File Ownership ---


class FileOwnershipMap(BaseModel):
    """Maps files to their owning agent role."""
    file_ownership: dict[str, str] = Field(default_factory=dict)
    shared_files: list[str] = Field(default_factory=list)
    conflict_resolution: str = "architect owns shared files, others request via HANDOFF.md"


# --- Execution Plan ---


class AgentTask(BaseModel):
    """A task assigned to a specific agent."""
    role: str
    description: str
    files_to_modify: list[str] = Field(default_factory=list)
    files_to_create: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class ExecutionPlan(BaseModel):
    """Plan produced by the planner agent."""
    task_id: str
    description: str
    rust_stack: RustStack = Field(default_factory=RustStack)
    file_ownership: FileOwnershipMap = Field(default_factory=FileOwnershipMap)
    agents: list[AgentTask] = Field(default_factory=list)
    estimated_cost_usd: float = 0.0
    estimated_duration_seconds: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


# --- Cost Tracking ---


class CostSnapshot(BaseModel):
    """Cost state at a point in time."""
    agent_role: str
    model: str = "sonnet"
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0


class TaskCostSummary(BaseModel):
    """Aggregate cost for an entire task."""
    task_id: str
    agents: list[CostSnapshot] = Field(default_factory=list)
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    total_duration_seconds: float = 0.0
    budget_usd: float = 15.0

    @property
    def budget_percent(self) -> float:
        if self.budget_usd <= 0:
            return 0.0
        return (self.total_cost_usd / self.budget_usd) * 100


# --- Dashboard ---


class AgentDashboardEntry(BaseModel):
    """One row in the Telegram progress dashboard."""
    role: str
    status: str = "waiting"         # waiting, running, done, error
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    tokens: int = 0


class TaskDashboard(BaseModel):
    """Full dashboard for Telegram display."""
    task_id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    agents: list[AgentDashboardEntry] = Field(default_factory=list)
    total_cost_usd: float = 0.0
    budget_usd: float = 15.0
    compile_ok: bool | None = None
    clippy_ok: bool | None = None
    tests_status: str = "pending"
    baseline_tests: int = 0
    current_tests: int = 0
    regressions: int = 0
