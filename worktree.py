"""Git worktree manager for agent isolation."""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

WORKTREE_DIR = ".cotg-worktrees"


async def _git(args: list[str], cwd: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run a git command."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", "git timeout"
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


class WorktreeManager:
    """Manages git worktrees for multi-agent task isolation."""

    def __init__(self, project_path: str, task_id: str):
        self.project_path = project_path
        self.task_id = task_id
        self.base_dir = os.path.join(project_path, WORKTREE_DIR, task_id)
        self.worktrees: dict[str, str] = {}  # role -> worktree_path

    async def create(self, role: str) -> str:
        """Create a worktree for an agent role, return its path."""
        branch_name = f"cotg/{self.task_id}/{role}"
        wt_path = os.path.join(self.base_dir, role)

        # Create base directory
        os.makedirs(self.base_dir, exist_ok=True)

        # Create branch from current HEAD
        rc, _, stderr = await _git(
            ["worktree", "add", "-b", branch_name, wt_path],
            cwd=self.project_path,
        )
        if rc != 0:
            # Branch might already exist — try without -b
            rc, _, stderr = await _git(
                ["worktree", "add", wt_path, branch_name],
                cwd=self.project_path,
            )
            if rc != 0:
                raise RuntimeError(f"Failed to create worktree for {role}: {stderr}")

        self.worktrees[role] = wt_path
        logger.info("Created worktree for %s at %s", role, wt_path)
        return wt_path

    async def remove(self, role: str) -> None:
        """Remove a worktree and its branch."""
        wt_path = self.worktrees.get(role)
        if not wt_path:
            return

        branch_name = f"cotg/{self.task_id}/{role}"

        # Remove worktree
        await _git(["worktree", "remove", "--force", wt_path], cwd=self.project_path)
        # Delete branch
        await _git(["branch", "-D", branch_name], cwd=self.project_path)

        self.worktrees.pop(role, None)
        logger.info("Removed worktree for %s", role)

    async def cleanup(self) -> None:
        """Remove all worktrees for this task."""
        for role in list(self.worktrees):
            await self.remove(role)

        # Remove task directory
        if os.path.exists(self.base_dir):
            await _git(["worktree", "prune"], cwd=self.project_path)
            try:
                os.rmdir(self.base_dir)
            except OSError:
                pass

    async def commit_agent_work(self, role: str, message: str) -> str | None:
        """Commit all changes in an agent's worktree, return commit hash."""
        wt_path = self.worktrees.get(role)
        if not wt_path:
            return None

        # Stage all changes
        rc, _, _ = await _git(["add", "-A"], cwd=wt_path)
        if rc != 0:
            return None

        # Check if there are changes to commit
        rc, stdout, _ = await _git(["status", "--porcelain"], cwd=wt_path)
        if not stdout.strip():
            return None  # Nothing to commit

        # Commit
        rc, _, stderr = await _git(
            ["commit", "-m", message], cwd=wt_path,
        )
        if rc != 0:
            logger.warning("Commit failed for %s: %s", role, stderr)
            return None

        # Get commit hash
        rc, stdout, _ = await _git(["rev-parse", "HEAD"], cwd=wt_path)
        return stdout.strip() if rc == 0 else None

    async def merge_to_integration(self, integration_branch: str) -> list[str]:
        """Merge all agent branches into the integration branch.

        Returns list of merge conflicts (empty = success).
        """
        conflicts = []

        # Create integration branch from main
        rc, _, _ = await _git(
            ["checkout", "-B", integration_branch],
            cwd=self.project_path,
        )
        if rc != 0:
            return ["Failed to create integration branch"]

        for role, wt_path in self.worktrees.items():
            branch_name = f"cotg/{self.task_id}/{role}"
            rc, stdout, stderr = await _git(
                ["merge", "--no-ff", branch_name, "-m", f"Merge {role} into integration"],
                cwd=self.project_path,
            )
            if rc != 0:
                conflicts.append(f"{role}: {stderr.strip()[:200]}")
                # Abort failed merge
                await _git(["merge", "--abort"], cwd=self.project_path)

        return conflicts

    def get_path(self, role: str) -> str | None:
        """Get the worktree path for a role."""
        return self.worktrees.get(role)
