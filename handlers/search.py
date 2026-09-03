from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from config import Settings
from services.database import DownloadRepository


def is_allowed(update: Update, settings: Settings) -> bool:
    user = update.effective_user
    if user is None:
        return False
    return user.id in settings.telegram_user_ids


def is_private_chat(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == "private"


def main_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔎 Buscar cancion", switch_inline_query_current_chat="")],
            [InlineKeyboardButton("📚 Buscar playlist", callback_data="playlist_prompt")],
        ]
    )


async def reject_unauthorized(update: Update) -> None:
    if update.effective_message is not None:
        await update.effective_message.reply_text("No tienes permiso para usar este bot.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    user = update.effective_user
    if not is_allowed(update, settings) or user is None:
        await reject_unauthorized(update)
        return
    if not is_private_chat(update):
        if update.effective_message is not None:
            await update.effective_message.reply_text("Usame desde mi chat privado.")
        return

    repository: DownloadRepository = context.application.bot_data["repository"]
    await repository.register_user(user)
    context.user_data["awaiting_playlist_url"] = False
    context.user_data["intro_prompt_sent"] = False

    await update.effective_message.reply_text(
        "Bienvenido. Elige que quieres buscar.",
        reply_markup=main_menu_markup(),
    )


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting_playlist_url"] = False
    text = "Elige que quieres buscar."
    query = update.callback_query
    if query is not None:
        try:
            await query.edit_message_text(text, reply_markup=main_menu_markup())
            return
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise

    message = update.effective_message
    if isinstance(message, Message):
        await message.reply_text(text, reply_markup=main_menu_markup())
