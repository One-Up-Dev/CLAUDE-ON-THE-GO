"""Telegram bot that bridges messages to Claude CLI."""

import asyncio
import logging
import time

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from claude_runner import run_claude, stream_claude
from config import Config
from db import save_message
from formatting import (
    format_response,
    is_plain_text,
    is_text_content,
    sanitize_output,
)

logger = logging.getLogger(__name__)


async def send_typing_periodically(
    chat_id: int, bot, stop_event: asyncio.Event
) -> None:
    """Send TYPING action every 4s until stop_event is set."""
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass


async def send_chunks(update: Update, chunks: list) -> None:
    """Send formatted content chunks to the user."""
    for item in chunks:
        try:
            if is_plain_text(item):
                await update.message.reply_text(
                    item,
                    disable_web_page_preview=True,
                )
            elif is_text_content(item):
                await update.message.reply_text(
                    item.content,
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=True,
                )
        except Exception as e:
            # Fallback: send as plain text without MarkdownV2
            logger.warning("Failed to send formatted chunk: %s", e)
            try:
                text = item if is_plain_text(item) else getattr(item, "content", str(item))
                await update.message.reply_text(text, disable_web_page_preview=True)
            except Exception as e2:
                logger.error("Failed to send chunk even as plain text: %s", e2)


async def handle_message(update: Update, context) -> None:
    """Handle incoming text messages: forward to Claude CLI and reply."""
    chat_id = update.message.chat_id
    user_text = update.message.text
    config: Config = context.bot_data["config"]

    logger.info("Message received from chat_id=%d (%d chars)", chat_id, len(user_text))
    save_message("user", user_text, source="telegram")

    # Start typing indicator
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        send_typing_periodically(chat_id, context.bot, stop_typing)
    )

    name = config.assistant_name
    try:
        response = await run_claude(user_text, config)
    except TimeoutError:
        await update.message.reply_text(
            f"Timeout : {name} n'a pas répondu en {config.claude_timeout} secondes."
        )
        return
    except RuntimeError as e:
        logger.error("%s error: %s", name, e)
        await update.message.reply_text(f"Erreur {name} : {e}")
        return
    except Exception as e:
        logger.error("Unexpected error running %s: %s", name, e)
        await update.message.reply_text(f"Erreur inattendue : {type(e).__name__}")
        return
    finally:
        stop_typing.set()
        await typing_task

    logger.info("%s responded (%d chars)", name, len(response))
    save_message("assistant", response, source="telegram")

    chunks = await format_response(response, config.max_message_length)
    await send_chunks(update, chunks)


async def handle_message_streaming(update: Update, context) -> None:
    """Handle incoming messages with real-time streaming to Telegram."""
    chat_id = update.message.chat_id
    user_text = update.message.text
    config: Config = context.bot_data["config"]

    # Fallback to batch mode if streaming disabled
    if not config.stream_enabled:
        return await handle_message(update, context)

    logger.info("Message received (stream) from chat_id=%d (%d chars)", chat_id, len(user_text))
    save_message("user", user_text, source="telegram")

    # Start typing indicator
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        send_typing_periodically(chat_id, context.bot, stop_typing)
    )

    # Send initial placeholder message
    streaming_msg = await update.message.reply_text(
        "...",
        disable_web_page_preview=True,
    )

    buffer = ""
    last_edit = 0.0
    display_limit = 4000  # Truncate display during stream, keep full buffer

    try:
        async for chunk in stream_claude(user_text, config):
            if chunk is None:
                # Stream complete
                break
            buffer += chunk

            now = time.monotonic()
            if now - last_edit >= config.stream_edit_interval:
                display = buffer[:display_limit]
                if len(buffer) > display_limit:
                    display += "\n\n... (streaming)"
                try:
                    await streaming_msg.edit_text(
                        display + config.stream_indicator,
                        disable_web_page_preview=True,
                    )
                    last_edit = now
                except Exception as e:
                    logger.warning("Stream edit failed: %s", e)

    except TimeoutError:
        stop_typing.set()
        await typing_task
        try:
            await streaming_msg.edit_text(f"Timeout : {config.assistant_name} n'a pas répondu à temps.")
        except Exception:
            pass
        return
    except RuntimeError as e:
        logger.error("Stream error: %s", e)
        stop_typing.set()
        await typing_task
        try:
            await streaming_msg.edit_text(f"Erreur {config.assistant_name} : {e}")
        except Exception:
            pass
        return
    except Exception as e:
        logger.error("Unexpected stream error: %s", e)
        stop_typing.set()
        await typing_task
        try:
            await streaming_msg.edit_text(f"Erreur inattendue : {type(e).__name__}")
        except Exception:
            pass
        return
    finally:
        stop_typing.set()
        await typing_task

    if not buffer.strip():
        try:
            await streaming_msg.edit_text(f"(Réponse vide de {config.assistant_name})")
        except Exception:
            pass
        return

    logger.info("Stream complete (%d chars)", len(buffer))
    save_message("assistant", buffer, source="telegram")

    # Delete the streaming message, send properly formatted response
    try:
        await streaming_msg.delete()
    except Exception:
        pass

    buffer = sanitize_output(buffer)
    chunks = await format_response(buffer, config.max_message_length)
    await send_chunks(update, chunks)


async def handle_start(update: Update, context) -> None:
    """Handle /start command."""
    name = context.bot_data["config"].assistant_name
    await update.message.reply_text(
        f"Salut ! Je suis {name}, ton assistant personnel. Envoie-moi un message !",
        disable_web_page_preview=True,
    )


async def error_handler(update: object, context) -> None:
    """Global error handler: log and notify user if possible."""
    logger.error("Unhandled exception: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.message:
        try:
            await update.message.reply_text(
                "Une erreur interne est survenue. Vérifie les logs."
            )
        except Exception:
            pass


def main() -> None:
    """Initialize and run the bot."""
    load_dotenv()

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    # Reduce noise from httpx
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config = Config.from_env()

    app = Application.builder().token(config.telegram_token).build()
    app.bot_data["config"] = config

    auth = filters.Chat(chat_id=config.allowed_chat_id)
    app.add_handler(CommandHandler("start", handle_start, filters=auth))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & auth, handle_message_streaming)
    )
    app.add_error_handler(error_handler)

    logger.info("Bot started, polling for updates (chat_id=%d)...", config.allowed_chat_id)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
