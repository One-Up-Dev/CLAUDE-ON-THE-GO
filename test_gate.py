"""Test pyramid runner — Level 0 to 3 with baseline comparison."""

import asyncio
import hashlib
import logging
import re
import time

from schemas import TestBaseline, TestDelta, TestLevel, TestResult

logger = logging.getLogger(__name__)

# Timeout per test level (seconds)
LEVEL_TIMEOUTS = {
    TestLevel.SMOKE: 30,
    TestLevel.FAST: 30,
    TestLevel.NORMAL: 120,
    TestLevel.FULL: 600,
}


async def _run_cmd(cmd: list[str], cwd: str, timeout: int) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"Timeout after {timeout}s"
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def capture_baseline(project_path: str) -> TestBaseline:
    """Capture a test baseline snapshot before a task begins."""
    # Get test list
    rc, stdout, stderr = await _run_cmd(
        ["cargo", "test", "--workspace", "--", "--list"],
        cwd=project_path, timeout=60,
    )
    if rc != 0:
        logger.warning("Failed to list tests: %s", stderr)
        return TestBaseline()

    # Count tests from --list output (lines ending with ": test")
    test_lines = [l for l in stdout.splitlines() if l.strip().endswith(": test")]
    total = len(test_lines)
    snapshot_hash = hashlib.sha256(stdout.encode()).hexdigest()[:16]

    # Run tests to get passing count
    rc, stdout, stderr = await _run_cmd(
        ["cargo", "test", "--workspace"],
        cwd=project_path, timeout=120,
    )
    passing = _count_passing(stdout + stderr)

    return TestBaseline(
        total_tests=total,
        passing_tests=passing,
        snapshot_hash=snapshot_hash,
    )


def _count_passing(output: str) -> int:
    """Parse cargo test output for passing count."""
    # Match: "test result: ok. X passed; Y failed; Z ignored"
    total_passed = 0
    for match in re.finditer(r"test result: \w+\.\s+(\d+) passed", output):
        total_passed += int(match.group(1))
    return total_passed


def _count_failed(output: str) -> int:
    """Parse cargo test output for failed count."""
    total_failed = 0
    for match in re.finditer(r"(\d+) failed", output):
        total_failed += int(match.group(1))
    return total_failed


def _extract_failed_tests(output: str) -> list[str]:
    """Extract names of failed tests from cargo test output."""
    failed = []
    for match in re.finditer(r"---- (\S+) stdout ----", output):
        failed.append(match.group(1))
    # Also match "FAILED" lines
    for match in re.finditer(r"test (\S+) \.\.\. FAILED", output):
        if match.group(1) not in failed:
            failed.append(match.group(1))
    return failed


def _extract_compiler_errors(output: str) -> list[str]:
    """Extract compiler error lines."""
    errors = []
    for line in output.splitlines():
        if line.startswith("error[") or line.startswith("error:"):
            errors.append(line.strip())
    return errors[:20]  # Cap at 20 errors


async def run_test_level(
    level: TestLevel, project_path: str, timeout: int | None = None
) -> TestResult:
    """Run a specific test level and return structured results."""
    t0 = time.monotonic()
    to = timeout or LEVEL_TIMEOUTS[level]

    if level == TestLevel.SMOKE:
        rc, stdout, stderr = await _run_cmd(
            ["cargo", "check", "--workspace"], cwd=project_path, timeout=to,
        )
        output = stdout + stderr
        compiler_errors = _extract_compiler_errors(output)
        return TestResult(
            level=level,
            passed=rc == 0,
            compiler_errors=compiler_errors,
            output=_compact_output(output),
            duration_seconds=time.monotonic() - t0,
        )

    if level == TestLevel.FAST:
        cmd = ["cargo", "test", "--lib"]
    elif level == TestLevel.NORMAL:
        cmd = ["cargo", "test", "--workspace"]
    else:  # FULL
        cmd = ["cargo", "test", "--workspace"]

    rc, stdout, stderr = await _run_cmd(cmd, cwd=project_path, timeout=to)
    output = stdout + stderr
    passed_count = _count_passing(output)
    failed_count = _count_failed(output)
    failed_names = _extract_failed_tests(output)

    result = TestResult(
        level=level,
        passed=rc == 0,
        total_tests=passed_count + failed_count,
        passed_tests=passed_count,
        failed_tests=failed_names,
        compiler_errors=_extract_compiler_errors(output),
        output=_compact_output(output),
        duration_seconds=time.monotonic() - t0,
    )

    # For FULL level, also run trunk build if applicable
    if level == TestLevel.FULL:
        trunk_rc, trunk_out, trunk_err = await _run_cmd(
            ["trunk", "build"], cwd=project_path, timeout=300,
        )
        if trunk_rc != 0:
            result.passed = False
            result.compiler_errors.append(f"trunk build failed: {trunk_err[:200]}")

    return result


def compare_to_baseline(baseline: TestBaseline, result: TestResult) -> TestDelta:
    """Compare test result against baseline, compute delta."""
    newly_failing = max(0, baseline.passing_tests - result.passed_tests)
    newly_added = max(0, result.total_tests - baseline.total_tests)
    return TestDelta(
        total_before=baseline.total_tests,
        total_after=result.total_tests,
        passing_before=baseline.passing_tests,
        passing_after=result.passed_tests,
        newly_failing=newly_failing,
        newly_added=newly_added,
    )


def format_compact(result: TestResult) -> str:
    """Format test result in compact ERROR/OK format for context injection."""
    if result.passed:
        return (
            f"OK: {result.passed_tests}/{result.total_tests} tests passing "
            f"({result.duration_seconds:.1f}s)"
        )
    lines = []
    for err in result.compiler_errors[:5]:
        lines.append(f"ERROR: {err}")
    for test_name in result.failed_tests[:5]:
        lines.append(f"ERROR: {test_name} — FAILED")
    if result.regressions > 0:
        lines.append(f"REGRESSION: {result.regressions} tests broke vs baseline")
    return "\n".join(lines) if lines else f"ERROR: tests failed ({result.output[:100]})"


def _compact_output(output: str, max_lines: int = 50) -> str:
    """Trim output to keep context window manageable."""
    lines = output.splitlines()
    if len(lines) <= max_lines:
        return output
    # Keep first 10 + last 40 lines
    return "\n".join(lines[:10] + ["... (truncated) ..."] + lines[-40:])
