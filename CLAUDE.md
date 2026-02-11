# Telegram Bot — Claude CLI Bridge

## Overview
Telegram bot that bridges user messages to Claude CLI (`claude -p`) and sends back formatted responses.

## Architecture
- `bot.py` — Telegram bot entry point (polling, handlers, typing indicator)
- `claude_runner.py` — Async subprocess wrapper for `claude -p`
- `config.py` — Configuration from environment variables
- `formatting.py` — Markdown → Telegram format conversion (using `telegramify-markdown`)

## Running
```bash
source .venv/bin/activate
python bot.py
```

## Environment
- Python 3.13+ with venv in `.venv/`
- Dependencies in `requirements.txt`
- Config via `.env` (see `.env.example`)

## Database
- `database.db` (SQLite) — Conversation history persistence
- Schema: `messages(id, role, content, metadata, source)`
- `source` column: `claude-code`, `telegram`, `web`, etc.
- Hooks in `.claude/hooks/` handle automatic save/load

## Key Conventions
- Claude CLI runs with `cwd` set to this project directory
- Bot restricted to a single `TELEGRAM_CHAT_ID` for security
- Responses formatted via `telegramify-markdown` with MarkdownV2 fallback
