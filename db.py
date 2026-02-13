"""Shared SQLite database module for conversation persistence."""

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = str(Path(__file__).resolve().parent / "database.db")
MAX_MESSAGES = 5000
_db_initialized: set[str] = set()


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if DB_PATH not in _db_initialized:
        _init_db(conn)
        _db_initialized.add(DB_PATH)
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
    conn.commit()


def _content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _is_duplicate(conn, role, c_hash):
    """Check if the last message with the same role has the same content hash."""
    row = conn.execute(
        "SELECT content_hash FROM messages WHERE role = ? ORDER BY id DESC LIMIT 1",
        (role,),
    ).fetchone()
    if not row:
        return False
    return row["content_hash"] == c_hash


def save_message(role, content, source="claude-code", session_id=""):
    """Save a message to the database with deduplication."""
    if not content or not content.strip():
        return
    try:
        conn = get_connection()
        try:
            c_hash = _content_hash(content)

            if _is_duplicate(conn, role, c_hash):
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
        finally:
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
