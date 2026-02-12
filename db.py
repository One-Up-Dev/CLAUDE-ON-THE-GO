"""Shared SQLite database module for conversation and multi-agent persistence."""

import hashlib
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = str(Path(__file__).resolve().parent / "database.db")
DEDUP_WINDOW_SECONDS = 60
MAX_MESSAGES = 5000


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _init_db(conn)
    return conn


def _init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            source TEXT DEFAULT 'claude-code',
            content_hash TEXT
        )
    """)
    # Add content_hash column if missing (migration)
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN content_hash TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_source ON messages (source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_role ON messages (role)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_id_desc ON messages (id DESC)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_hash ON messages (role, content_hash)"
    )

    # Multi-agent tables
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            project_path TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            plan_json TEXT,
            rust_stack_json TEXT,
            file_ownership_json TEXT,
            integration_branch TEXT,
            retry_count INTEGER DEFAULT 0,
            total_cost_usd REAL DEFAULT 0.0,
            total_tokens INTEGER DEFAULT 0,
            telegram_message_id INTEGER,
            created_at TEXT,
            updated_at TEXT,
            completed_at TEXT,
            error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT REFERENCES tasks(id),
            role TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            worktree_path TEXT,
            branch_name TEXT,
            model TEXT,
            prompt TEXT,
            output TEXT,
            cost_usd REAL DEFAULT 0.0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            duration_seconds REAL DEFAULT 0.0,
            files_modified TEXT,
            level1_passed INTEGER,
            regressions INTEGER DEFAULT 0,
            attempt INTEGER DEFAULT 1,
            error TEXT,
            started_at TEXT,
            completed_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS test_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT REFERENCES tasks(id),
            agent_run_id INTEGER REFERENCES agent_runs(id),
            test_level INTEGER NOT NULL,
            passed INTEGER DEFAULT 0,
            total_tests INTEGER DEFAULT 0,
            passed_tests INTEGER DEFAULT 0,
            failed_tests TEXT,
            output TEXT,
            compiler_errors TEXT,
            regressions INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS regression_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT REFERENCES tasks(id),
            agent_role TEXT,
            tests_before INTEGER,
            tests_after INTEGER,
            regressions INTEGER,
            new_tests INTEGER,
            regression_rate REAL,
            created_at TEXT
        )
    """)
    conn.commit()


def _content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _is_duplicate(conn, role, c_hash):
    """Check if a message with the same hash exists in the dedup window."""
    row = conn.execute(
        """SELECT metadata FROM messages
           WHERE role = ? AND content_hash = ?
           ORDER BY id DESC LIMIT 1""",
        (role, c_hash),
    ).fetchone()
    if not row or not row[0]:
        return False
    try:
        meta = json.loads(row[0])
        last_time = meta.get("created_at", "")
        if not last_time:
            return False
        last_dt = datetime.fromisoformat(last_time.replace("Z", "+00:00"))
        now_dt = datetime.now(timezone.utc)
        return abs((now_dt - last_dt).total_seconds()) < DEDUP_WINDOW_SECONDS
    except (json.JSONDecodeError, ValueError):
        return False


def save_message(role, content, source="claude-code", session_id=""):
    """Save a message to the database with deduplication."""
    if not content or not content.strip():
        return
    try:
        conn = get_connection()
        c_hash = _content_hash(content)

        if _is_duplicate(conn, role, c_hash):
            conn.close()
            return

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        metadata = json.dumps({
            "session_id": session_id,
            "created_at": now,
            "content_hash": c_hash,
        })
        conn.execute(
            "INSERT INTO messages (role, content, metadata, source, content_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (role, content, metadata, source, c_hash),
        )
        conn.commit()
        _maybe_rotate(conn)
        conn.close()
    except (sqlite3.DatabaseError, sqlite3.OperationalError) as e:
        logger.warning("Failed to save message: %s", e)


def _maybe_rotate(conn):
    """Keep only the last MAX_MESSAGES rows."""
    try:
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if count > MAX_MESSAGES:
            conn.execute(
                """DELETE FROM messages WHERE id NOT IN (
                    SELECT id FROM messages ORDER BY id DESC LIMIT ?
                )""",
                (MAX_MESSAGES,),
            )
            conn.commit()
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        pass


# --- Task CRUD ---


def create_task(project_path, description):
    """Create a new task, return its ID."""
    task_id = uuid.uuid4().hex[:12]
    now = _now_iso()
    conn = get_connection()
    conn.execute(
        "INSERT INTO tasks (id, project_path, description, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'pending', ?, ?)",
        (task_id, project_path, description, now, now),
    )
    conn.commit()
    conn.close()
    return task_id


def update_task(task_id, **kwargs):
    """Update task fields. Pass column=value as kwargs."""
    kwargs["updated_at"] = _now_iso()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [task_id]
    conn = get_connection()
    conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def get_task(task_id):
    """Get a task by ID, return dict or None."""
    conn = get_connection()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_tasks(status=None, limit=20):
    """List recent tasks, optionally filtered by status."""
    conn = get_connection()
    if status:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Agent Run CRUD ---


def create_agent_run(task_id, role, model="sonnet", branch_name="", worktree_path=""):
    """Create a new agent run, return its ID."""
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO agent_runs (task_id, role, status, model, branch_name, worktree_path, started_at) "
        "VALUES (?, ?, 'running', ?, ?, ?, ?)",
        (task_id, role, model, branch_name, worktree_path, _now_iso()),
    )
    run_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return run_id


def update_agent_run(run_id, **kwargs):
    """Update agent run fields."""
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [run_id]
    conn = get_connection()
    conn.execute(f"UPDATE agent_runs SET {sets} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def get_agent_runs(task_id):
    """Get all agent runs for a task."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM agent_runs WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Test Results ---


def save_test_result(task_id, test_level, passed, total_tests=0, passed_tests=0,
                     failed_tests="", output="", compiler_errors="", regressions=0,
                     agent_run_id=None):
    """Save a test result."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO test_results (task_id, agent_run_id, test_level, passed, "
        "total_tests, passed_tests, failed_tests, output, compiler_errors, "
        "regressions, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, agent_run_id, test_level, int(passed), total_tests, passed_tests,
         failed_tests, output, compiler_errors, regressions, _now_iso()),
    )
    conn.commit()
    conn.close()


# --- Regression Log ---


def log_regression(task_id, agent_role, tests_before, tests_after,
                   regressions, new_tests):
    """Log a regression event."""
    rate = regressions / tests_before if tests_before > 0 else 0.0
    conn = get_connection()
    conn.execute(
        "INSERT INTO regression_log (task_id, agent_role, tests_before, tests_after, "
        "regressions, new_tests, regression_rate, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, agent_role, tests_before, tests_after, regressions, new_tests,
         rate, _now_iso()),
    )
    conn.commit()
    conn.close()
