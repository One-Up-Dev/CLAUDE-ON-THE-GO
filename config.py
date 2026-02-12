"""Bot configuration from environment variables."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_chat_id: int
    claude_bin: str = os.path.expanduser("~/.local/bin/claude")
    claude_cwd: str = os.path.join(os.path.dirname(os.path.abspath(__file__)))
    claude_timeout: int = 300
    max_message_length: int = 4090
    # Multi-agent orchestrator settings
    build_budget_usd: float = 15.0
    build_max_retries: int = 3

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("TELEGRAM_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token:
            raise ValueError("TELEGRAM_TOKEN environment variable is required")
        if not chat_id:
            raise ValueError("TELEGRAM_CHAT_ID environment variable is required")
        return cls(
            telegram_token=token,
            allowed_chat_id=int(chat_id),
        )
