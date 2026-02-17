"""Bot configuration from environment variables."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_chat_id: int
    assistant_name: str = "Nova"
    system_prompt: str = ""
    claude_bin: str = os.path.expanduser("~/.local/bin/claude")
    claude_cwd: str = os.path.join(os.path.dirname(os.path.abspath(__file__)))
    claude_timeout: int = 300
    max_message_length: int = 4090
    stream_enabled: bool = True
    stream_edit_interval: float = 1.5
    stream_indicator: str = " ▍"

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("TELEGRAM_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token:
            raise ValueError("TELEGRAM_TOKEN environment variable is required")
        if not chat_id:
            raise ValueError("TELEGRAM_CHAT_ID environment variable is required")
        name = os.environ.get("ASSISTANT_NAME", "Nova")
        system_prompt = os.environ.get(
            "SYSTEM_PROMPT",
            f"You are {name}, a personal AI assistant for Oneup. "
            f"You always identify yourself as {name}, never as Claude or Claude Code. "
            "You communicate in French by default. "
            "You are helpful, concise, and friendly. "
            "This is an ongoing conversation — do NOT greet the user or re-introduce yourself "
            "at the start of each message. Do NOT re-examine code or state you already checked "
            "in the conversation history. Pick up naturally where you left off.",
        )
        return cls(
            telegram_token=token,
            allowed_chat_id=int(chat_id),
            assistant_name=name,
            system_prompt=system_prompt,
        )
