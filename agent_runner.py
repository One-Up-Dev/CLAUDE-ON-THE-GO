"""Multi-agent Claude CLI spawner with context budget management."""

import asyncio
import json
import logging
import os
import re
import time

from config import Config
from db import create_agent_run, update_agent_run
from schemas import AgentConfig, AgentResult, AgentStatus

logger = logging.getLogger(__name__)

# Agent prompt suffix injected into every agent
RESULT_BLOCK_INSTRUCTION = """
When you are done, output a summary block in EXACTLY this format:

## RESULT
STATUS: success|error
FILES_MODIFIED: file1.rs, file2.rs
TESTS_ADDED: 0
ERRORS: none
"""


class AgentRunner:
    """Spawns and manages a single Claude CLI agent process."""

    def __init__(self, config: Config, agent_config: AgentConfig, task_id: str):
        self.config = config
        self.agent_config = agent_config
        self.task_id = task_id
        self.run_id: int | None = None
        self.process: asyncio.subprocess.Process | None = None

    async def run(self, prompt: str, cwd: str,
                  handoff_context: str = "", file_ownership: str = "",
                  error_context: str = "") -> AgentResult:
        """Execute the agent and return structured results."""
        # Load agent system prompt from file
        system_prompt = self._load_agent_prompt()

        # Build context injection
        context_parts = []
        if handoff_context:
            context_parts.append(f"## HANDOFF\n{handoff_context}")
        if file_ownership:
            context_parts.append(f"## FILE OWNERSHIP\n{file_ownership}")
        if error_context:
            context_parts.append(f"## PREVIOUS ERRORS (fix these)\n{error_context}")
        context_parts.append(RESULT_BLOCK_INSTRUCTION)

        full_system = system_prompt + "\n\n" + "\n\n".join(context_parts)

        # Build command
        cmd = [
            self.config.claude_bin,
            "-p", prompt,
            "--output-format", "json",
            "--dangerously-skip-permissions",
            "--model", self.agent_config.model,
            "--append-system-prompt", full_system,
        ]
        if self.agent_config.budget_usd > 0:
            cmd.extend(["--max-turns", "50"])

        # Record run in DB
        self.run_id = create_agent_run(
            task_id=self.task_id,
            role=self.agent_config.role,
            model=self.agent_config.model,
            worktree_path=cwd,
        )

        logger.info(
            "Starting agent %s (model=%s, cwd=%s)",
            self.agent_config.role, self.agent_config.model, cwd,
        )

        t0 = time.monotonic()
        try:
            result = await self._execute(cmd, cwd)
            duration = time.monotonic() - t0
            result.duration_seconds = duration

            # Update DB
            update_agent_run(
                self.run_id,
                status=result.status.value,
                output=result.raw_output[:10000],
                cost_usd=result.cost_usd,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                duration_seconds=duration,
                files_modified=",".join(result.files_modified),
                completed_at=__import__("db")._now_iso(),
            )

            return result

        except Exception as e:
            duration = time.monotonic() - t0
            update_agent_run(
                self.run_id,
                status="failed",
                error=str(e)[:500],
                duration_seconds=duration,
                completed_at=__import__("db")._now_iso(),
            )
            return AgentResult(
                status=AgentStatus.FAILED,
                errors=[str(e)],
                duration_seconds=duration,
            )

    async def _execute(self, cmd: list[str], cwd: str) -> AgentResult:
        """Run claude CLI process and parse output."""
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env={**os.environ, "NO_COLOR": "1"},
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                self.process.communicate(),
                timeout=self.agent_config.timeout,
            )
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()
            return AgentResult(
                status=AgentStatus.FAILED,
                errors=[f"Timeout after {self.agent_config.timeout}s"],
            )

        if stderr:
            logger.debug("Agent %s stderr: %s", self.agent_config.role,
                         stderr.decode(errors="replace")[:500])

        raw = stdout.decode(errors="replace").strip()
        if not raw:
            return AgentResult(
                status=AgentStatus.FAILED,
                errors=["Empty output from claude"],
            )

        # Parse JSON output
        try:
            data = json.loads(raw)
            result_text = data.get("result", "")
            input_tokens = data.get("input_tokens", 0) or _extract_tokens(data, "input")
            output_tokens = data.get("output_tokens", 0) or _extract_tokens(data, "output")
            cost = data.get("cost_usd", 0.0)
        except json.JSONDecodeError:
            result_text = raw
            input_tokens = 0
            output_tokens = 0
            cost = 0.0

        # Parse RESULT block from agent output
        agent_result = _parse_result_block(result_text)
        agent_result.raw_output = result_text
        agent_result.input_tokens = input_tokens
        agent_result.output_tokens = output_tokens
        agent_result.cost_usd = cost

        return agent_result

    def _load_agent_prompt(self) -> str:
        """Load agent definition from its prompt file."""
        path = self.agent_config.prompt_file
        if not os.path.exists(path):
            logger.warning("Agent prompt file not found: %s", path)
            return f"You are the {self.agent_config.role} agent."
        with open(path) as f:
            return f.read()

    async def kill(self):
        """Kill the running agent process."""
        if self.process and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()


def _parse_result_block(text: str) -> AgentResult:
    """Parse the structured RESULT block from agent output."""
    result = AgentResult()

    # Find ## RESULT block
    match = re.search(r"## RESULT\s*\n(.*?)(?:\n##|\Z)", text, re.DOTALL)
    if not match:
        # No structured output — infer from text
        result.status = AgentStatus.SUCCESS
        return result

    block = match.group(1)

    # Parse STATUS
    status_match = re.search(r"STATUS:\s*(\w+)", block)
    if status_match:
        status_str = status_match.group(1).lower()
        result.status = (
            AgentStatus.SUCCESS if status_str == "success" else AgentStatus.FAILED
        )

    # Parse FILES_MODIFIED
    files_match = re.search(r"FILES_MODIFIED:\s*(.+)", block)
    if files_match:
        files = [f.strip() for f in files_match.group(1).split(",") if f.strip()]
        result.files_modified = [f for f in files if f.lower() != "none"]

    # Parse TESTS_ADDED
    tests_match = re.search(r"TESTS_ADDED:\s*(\d+)", block)
    if tests_match:
        result.tests_added = int(tests_match.group(1))

    # Parse ERRORS
    errors_match = re.search(r"ERRORS:\s*(.+)", block)
    if errors_match:
        err_text = errors_match.group(1).strip()
        if err_text.lower() != "none":
            result.errors = [err_text]

    return result


def _extract_tokens(data: dict, token_type: str) -> int:
    """Try to extract token counts from various JSON output formats."""
    # Claude CLI JSON output format may vary
    usage = data.get("usage", {})
    if token_type == "input":
        return usage.get("input_tokens", 0)
    return usage.get("output_tokens", 0)
