# COTG — Claude On The Go

Access [Claude Code](https://docs.anthropic.com/en/docs/claude-code) from anywhere, 24/7, through Telegram.

COTG is a lightweight bridge between Telegram and the Claude CLI (`claude -p`). Send a message on Telegram, get a Claude Code response — streamed in real time — from your phone, tablet, or any device.

## Features

- **Real-time streaming** — responses appear progressively as Claude generates them, with a live cursor indicator
- **Claude Code via Telegram** — full CLI capabilities from your phone
- **Markdown formatting** — responses are converted to Telegram-compatible MarkdownV2 via `telegramify-markdown`
- **Conversation history** — messages are persisted in SQLite with deduplication and auto-rotation (5000 messages)
- **Configurable identity** — the bot has its own persona (default: "Nova"), separate from Claude Code
- **Output sanitization** — strips @mentions and masks leaked API keys/tokens before sending
- **Typing indicator** — visual feedback while Claude is thinking
- **Single-user security** — restricted to one authorized chat ID
- **Systemd service** — runs as a background daemon with auto-restart

## Architecture

```
Telegram → bot.py → stream_claude() → claude -p (stream-json) → formatting.py → Telegram
                                                                       ↕
                                                                     db.py → database.db
```

| File | Role |
|------|------|
| `bot.py` | Telegram bot entry point — polling, handlers, streaming message updates |
| `claude_runner.py` | Async subprocess wrapper — `run_claude()` (batch) and `stream_claude()` (streaming) |
| `config.py` | Configuration from environment variables (identity, streaming params, timeouts) |
| `formatting.py` | Markdown → Telegram conversion, output sanitization (secrets, mentions) |
| `db.py` | Shared SQLite module — conversation persistence, deduplication, auto-rotation |
| `.claude/hooks/` | Auto-save/load conversation history for Claude Code sessions |

## Prerequisites

- Python 3.13+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Installation

```bash
git clone git@github.com:One-Up-Dev/cotg.git
cd cotg

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_TOKEN` | Yes | — | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Yes | — | Authorized chat ID (single-user restriction) |
| `ASSISTANT_NAME` | No | `Nova` | Bot persona name (injected into system prompt) |
| `SYSTEM_PROMPT` | No | Auto-generated | Custom system prompt (overrides the default identity prompt) |

To get your chat ID, send a message to your bot and check:
```bash
curl https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```

## Usage

```bash
source .venv/bin/activate
python bot.py
```

### Run as a systemd service

Edit `telegram-bot.service` to match your paths, then:

```bash
sudo cp telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-bot.service
```

Check status:
```bash
sudo systemctl status telegram-bot.service
journalctl -u telegram-bot.service -f
```

## Streaming

Streaming is enabled by default. Claude's responses are streamed via `claude -p --output-format stream-json` and progressively edited in the Telegram message as text arrives. The final response is then reformatted with full Markdown rendering.

Configuration in `config.py`:
- `stream_enabled` — toggle streaming on/off (default: `True`)
- `stream_edit_interval` — seconds between Telegram message edits (default: `1.5`)
- `stream_indicator` — cursor shown during streaming (default: ` ▍`)

## Security

- Bot restricted to a single `TELEGRAM_CHAT_ID` — validated on every handler
- Output sanitization masks leaked API keys (OpenAI, GitHub, Slack, GitLab, Telegram tokens)
- Mass @mentions are neutralized with zero-width spaces
- URL web preview disabled on all messages to prevent data exfiltration
- Subprocess execution uses `create_subprocess_exec` (never `shell=True`)
- See `.claude/rules/security.md` for full security policy

## License

MIT
