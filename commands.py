"""Telegram command handlers for multi-agent builds."""

import logging
import os

from telegram import Update
from telegram.constants import ChatAction

from config import Config
from db import get_task, list_tasks
from orchestrator import Orchestrator, format_dashboard

logger = logging.getLogger(__name__)


async def handle_build(update: Update, context) -> None:
    """Handle /build <project_path> <task description>.

    Usage: /build /path/to/rust/project Add user authentication with JWT
    """
    config: Config = context.bot_data["config"]
    text = update.message.text.strip()

    # Parse: /build <path> <description>
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text(
            "Usage: /build <project_path> <task description>\n"
            "Example: /build ~/my-rust-app Add user auth with JWT",
            disable_web_page_preview=True,
        )
        return

    project_path = os.path.expanduser(parts[1])
    description = parts[2]

    # Validate project path
    if not os.path.isdir(project_path):
        await update.message.reply_text(f"Project path not found: {project_path}")
        return

    cargo_toml = os.path.join(project_path, "Cargo.toml")
    if not os.path.isfile(cargo_toml):
        await update.message.reply_text(f"No Cargo.toml found in {project_path}")
        return

    # Send initial status message
    status_msg = await update.message.reply_text(
        f"Starting build: {description}\nProject: {project_path}\n\nPlanning...",
        disable_web_page_preview=True,
    )

    # Progress callback — edits the status message
    async def on_progress(dashboard):
        try:
            text = format_dashboard(dashboard)
            await status_msg.edit_text(text, disable_web_page_preview=True)
        except Exception as e:
            logger.debug("Failed to update progress message: %s", e)

    # Budget alert callback
    async def on_budget_alert(task_id, percent, cost, budget):
        icon = "\u26a0\ufe0f" if percent < 100 else "\U0001f6d1"
        await update.message.reply_text(
            f"{icon} Budget alert: {percent}% (${cost:.2f}/${budget:.2f}) — task {task_id}",
            disable_web_page_preview=True,
        )

    # Run orchestrator
    orchestrator = Orchestrator(
        config=config,
        project_path=project_path,
        description=description,
        on_progress=on_progress,
        on_budget_alert=on_budget_alert,
    )

    dashboard = await orchestrator.execute()

    # Final status
    final_text = format_dashboard(dashboard)
    try:
        await status_msg.edit_text(final_text, disable_web_page_preview=True)
    except Exception:
        await update.message.reply_text(final_text, disable_web_page_preview=True)


async def handle_status(update: Update, context) -> None:
    """Handle /status [task_id] — show task dashboard.

    Usage:
        /status          — show recent tasks
        /status <id>     — show specific task details
    """
    text = update.message.text.strip()
    parts = text.split(maxsplit=1)

    if len(parts) > 1:
        # Specific task
        task_id = parts[1].strip()
        task = get_task(task_id)
        if not task:
            await update.message.reply_text(f"Task not found: {task_id}")
            return

        lines = [
            f"Task: {task['id']}",
            f"Status: {task['status']}",
            f"Description: {task['description'][:100]}",
            f"Project: {task['project_path']}",
            f"Cost: ${task['total_cost_usd']:.2f}",
            f"Created: {task['created_at'][:19] if task['created_at'] else 'N/A'}",
        ]
        if task["error"]:
            lines.append(f"Error: {task['error'][:200]}")
        await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)
    else:
        # List recent tasks
        tasks = list_tasks(limit=10)
        if not tasks:
            await update.message.reply_text("No tasks found.")
            return

        lines = ["Recent tasks:"]
        for t in tasks:
            status_icon = {
                "pending": "\u23f8", "planning": "\U0001f4cb",
                "executing": "\u23f3", "testing": "\U0001f9ea",
                "merging": "\U0001f500", "done": "\u2713",
                "error": "\u2717", "cancelled": "\u274c",
            }.get(t["status"], "?")
            cost = f"${t['total_cost_usd']:.2f}" if t["total_cost_usd"] else ""
            lines.append(f"{status_icon} {t['id']} — {t['description'][:40]} {cost}")

        await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)
