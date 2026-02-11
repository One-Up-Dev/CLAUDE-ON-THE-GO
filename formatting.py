"""Convert Claude's markdown output to Telegram-compatible format."""

import logging
import re

from telegramify_markdown import (
    Text,
    telegramify,
)
from telegramify_markdown.interpreters import TextInterpreter

logger = logging.getLogger(__name__)

# Only TextInterpreter: code blocks stay inline with copy button on mobile
_interpreters = [TextInterpreter()]


_MASS_MENTION_RE = re.compile(r"@(all|everyone|here|channel)\b", re.IGNORECASE)
_SECRET_PATTERNS_RE = re.compile(
    r"(sk-[a-zA-Z0-9]{20,})"          # OpenAI keys
    r"|(ghp_[a-zA-Z0-9]{36,})"        # GitHub PAT
    r"|(gho_[a-zA-Z0-9]{36,})"        # GitHub OAuth
    r"|(xoxb-[a-zA-Z0-9\-]{20,})"     # Slack bot token
    r"|(xoxp-[a-zA-Z0-9\-]{20,})"     # Slack user token
    r"|(glpat-[a-zA-Z0-9\-]{20,})"    # GitLab PAT
    r"|(\b[0-9]{9,}:[A-Za-z0-9_-]{30,})"  # Telegram bot token
)


def sanitize_output(text: str) -> str:
    """Strip mass mentions and mask leaked secrets."""
    text = _MASS_MENTION_RE.sub("@\u200B\\1", text)
    text = _SECRET_PATTERNS_RE.sub("[REDACTED]", text)
    return text


async def format_response(text: str, max_length: int = 4090) -> list:
    """Convert markdown text to Telegram-ready content chunks.

    Returns list of Text objects from telegramify,
    or list of plain strings as fallback.
    """
    text = sanitize_output(text)
    try:
        return await telegramify(
            text,
            max_word_count=max_length,
            interpreters_use=_interpreters,
        )
    except Exception as e:
        logger.warning("Markdown conversion failed, falling back to plain text: %s", e)
        return _fallback_split(text, max_length)


def _fallback_split(text: str, max_length: int) -> list[str]:
    """Simple split fallback when telegramify fails.

    Split at paragraph boundaries, then newlines, then hard-cut.
    Returns plain strings (not content objects).
    """
    if len(text) <= max_length:
        return [text]

    chunks = []
    current = ""

    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph

        if len(candidate) <= max_length:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(paragraph) > max_length:
            for line in paragraph.split("\n"):
                line_candidate = f"{current}\n{line}" if current else line
                if len(line_candidate) <= max_length:
                    current = line_candidate
                else:
                    if current:
                        chunks.append(current)
                    while len(line) > max_length:
                        chunks.append(line[:max_length])
                        line = line[max_length:]
                    current = line
        else:
            current = paragraph

    if current:
        chunks.append(current)

    return chunks


def is_plain_text(item) -> bool:
    """Check if a content item is a plain text fallback string."""
    return isinstance(item, str)


def is_text_content(item) -> bool:
    return isinstance(item, Text)


