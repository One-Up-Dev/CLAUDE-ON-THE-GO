#!/usr/bin/env python3
"""Save Claude Code messages to SQLite database.

Handles both UserPromptSubmit and Stop events.
Uses content hash + timestamp window for deduplication.
"""

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = str(Path(__file__).resolve().parent.parent.parent / "database.db")
DEDUP_WINDOW_SECONDS = 5


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            source TEXT DEFAULT 'claude-code'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_source ON messages (source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_role ON messages (role)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_id_desc ON messages (id DESC)")
    conn.commit()


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def is_duplicate(conn, role, c_hash, source, now_iso):
    """Check if a message with the same hash was saved within the dedup window."""
    row = conn.execute(
        """SELECT metadata FROM messages
           WHERE role = ? AND source = ?
           ORDER BY id DESC LIMIT 1""",
        (role, source),
    ).fetchone()
    if not row or not row[0]:
        return False
    try:
        meta = json.loads(row[0])
        last_hash = meta.get("content_hash", "")
        if last_hash != c_hash:
            return False
        last_time = meta.get("created_at", "")
        if not last_time:
            return False
        last_dt = datetime.fromisoformat(last_time.replace("Z", "+00:00"))
        now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        return abs((now_dt - last_dt).total_seconds()) < DEDUP_WINDOW_SECONDS
    except (json.JSONDecodeError, ValueError):
        return False


def save_message(role, session_id, content, source="claude-code"):
    if not content or not content.strip():
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        init_db(conn)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        c_hash = content_hash(content)

        if is_duplicate(conn, role, c_hash, source, now):
            conn.close()
            return

        metadata = json.dumps({
            "session_id": session_id,
            "created_at": now,
            "content_hash": c_hash,
        })
        conn.execute(
            "INSERT INTO messages (role, content, metadata, source) VALUES (?, ?, ?, ?)",
            (role, content, metadata, source),
        )
        conn.commit()
        conn.close()
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        pass


def main():
    data = json.load(sys.stdin)
    session_id = data.get("session_id", "unknown")
    event = data.get("hook_event_name", "")

    if event == "UserPromptSubmit":
        content = data.get("prompt", "")
        if content:
            save_message("user", session_id, content)

    elif event == "Stop":
        response = data.get("stop_hook_active_response", "")
        if response:
            save_message("assistant", session_id, response)
        else:
            transcript_path = data.get("transcript_path", "")
            if transcript_path and os.path.exists(transcript_path):
                _save_from_transcript(session_id, transcript_path)

    json.dump({"continue": True}, sys.stdout)


def _save_from_transcript(session_id, transcript_path):
    """Fallback: extract assistant messages from JSONL transcript."""
    try:
        with open(transcript_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("type") == "assistant":
                        msg = entry.get("message", {})
                        content_parts = msg.get("content", []) if isinstance(msg, dict) else []
                        text_parts = []
                        for part in content_parts:
                            if isinstance(part, str):
                                text_parts.append(part)
                            elif isinstance(part, dict) and part.get("type") == "text":
                                text_parts.append(part.get("text", ""))
                        if text_parts:
                            full_text = "\n".join(text_parts)
                            save_message("assistant", session_id, full_text)
                except json.JSONDecodeError:
                    continue
    except (IOError, OSError):
        pass


if __name__ == "__main__":
    main()
