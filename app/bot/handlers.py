"""Обработчики бота. Бот работает только «на запись»: отдать медиа назад он не умеет."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from app.bot.middlewares import AccessMiddleware, AlbumMiddleware, is_photo_message
from app.config import Settings
from app.db import WhitelistRepository
from app.sessions import PhotoItem, SessionStore

logger = logging.getLogger(__name__)

router = Router(name="main")

HELP_TEXT = (
    "Я шлюз загрузки фотографий в Google Photos.\n\n"
    "Как пользоваться:\n"
    "1. Пришлите мне фото — одно или альбомом.\n"
    "2. Нажмите «📂 Выбрать альбом» и выберите существующий альбом "
    "или создайте новый.\n"
    "3. После успешной загрузки я удалю фотографии из чата, чтобы не копить историю.\n\n"
    "Важно: бот работает только в одну сторону — забрать или посмотреть "
    "загруженные фото через бота нельзя."
)

ADMIN_HELP = (
    "\n\nКоманды администратора:\n"
    "/whitelist_add <id> — выдать доступ\n"
    "/whitelist_remove <id> — отозвать доступ\n"
    "/whitelist_list — список доступов"
)

ONLY_PRIVATE = (
    "Выбор альбома работает только в личном чате с ботом "
    "(Telegram не разрешает кнопки Mini App в группах). Напишите мне напрямую."
)

NOT_A_PHOTO = (
    "Я принимаю только фотографии (в том числе отправленные файлом без сжатия). "
    "Пришлите фото, и я предложу выбрать альбом."
)


def setup_routers(
    *,
    whitelist: WhitelistRepository,
    settings: Settings,
) -> Router:
    """Навешивает middleware на роутер и возвращает его."""
    router.message.outer_middleware(AccessMiddleware(whitelist, settings))
    router.message.middleware(
        AlbumMiddleware(
            latency=settings.album_debounce_seconds,
            max_wait=settings.album_debounce_max_wait,
        )
    )
    return router


# ---------------------------------------------------------------- команды


@router.message(CommandStart())
async def cmd_start(message: Message, settings: Settings, is_admin: bool = False) -> None:
    text = HELP_TEXT + (ADMIN_HELP if is_admin else "")
    await message.answer(text)


@router.message(Command("help"))
async def cmd_help(message: Message, is_admin: bool = False) -> None:
    await message.answer(HELP_TEXT + (ADMIN_HELP if is_admin else ""))


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    await message.answer(f"Ваш Telegram ID: {user_id}")


@router.message(Command("whitelist_add"))
async def cmd_whitelist_add(
    message: Message,
    command: CommandObject,
    whitelist: WhitelistRepository,
    settings: Settings,
    is_admin: bool = False,
) -> None:
    if not is_admin:
        await message.answer("[HTTP 403] Forbidden: Команда доступна только администратору.")
        return

    target = _parse_user_id(command.args)
    if target is None:
        await message.answer(
            "[HTTP 400] Bad Request: Ожидается числовой telegram_user_id.\n"
            "Пример: /whitelist_add 123456789"
        )
        return

    added = await whitelist.add(target, added_by=settings.admin_id)
    if added:
        await message.answer(f"✅ Пользователь {target} добавлен в белый список.")
    else:
        await message.answer(f"ℹ️ Пользователь {target} уже был в белом списке.")


@router.message(Command("whitelist_remove"))
async def cmd_whitelist_remove(
    message: Message,
    command: CommandObject,
    whitelist: WhitelistRepository,
    settings: Settings,
    is_admin: bool = False,
) -> None:
    if not is_admin:
        await message.answer("[HTTP 403] Forbidden: Команда доступна только администратору.")
        return

    target = _parse_user_id(command.args)
    if target is None:
        await message.answer(
            "[HTTP 400] Bad Request: Ожидается числовой telegram_user_id.\n"
            "Пример: /whitelist_remove 123456789"
        )
        return

    if target == settings.admin_id:
        await message.answer("[HTTP 400] Bad Request: Администратора нельзя удалить из списка.")
        return

    removed = await whitelist.remove(target)
    if removed:
        await message.answer(f"✅ Доступ пользователя {target} отозван.")
    else:
        await message.answer(f"ℹ️ Пользователя {target} не было в белом списке.")


@router.message(Command("whitelist_list"))
async def cmd_whitelist_list(
    message: Message,
    whitelist: WhitelistRepository,
    settings: Settings,
    is_admin: bool = False,
) -> None:
    if not is_admin:
        await message.answer("[HTTP 403] Forbidden: Команда доступна только администратору.")
        return

    rows = await whitelist.list_all()
    if not rows:
        await message.answer("Белый список пуст.")
        return

    lines = ["Белый список:"]
    for user_id, added_at in rows:
        suffix = " (администратор)" if user_id == settings.admin_id else ""
        lines.append(f"• {user_id} — добавлен {added_at}{suffix}")
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------- фото


@router.message(F.photo | F.document)
async def on_photo(
    message: Message,
    store: SessionStore,
    settings: Settings,
    album: list[Message] | None = None,
) -> None:
    if not is_photo_message(message):
        await message.answer(NOT_A_PHOTO)
        return

    if message.chat.type != "private":
        await message.answer(ONLY_PRIVATE)
        return

    messages = album or [message]
    items = [item for item in (_to_photo_item(msg) for msg in messages) if item is not None]
    if not items:
        await message.answer("[HTTP 400] Bad Request: Некорректный или повреждённый медиафайл.")
        return

    session = await store.create(
        user_id=message.from_user.id if message.from_user else 0,
        chat_id=message.chat.id,
        items=items,
        media_group_id=message.media_group_id,
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📂 Выбрать альбом",
                    web_app=WebAppInfo(url=settings.webapp_link(session.session_id)),
                )
            ]
        ]
    )

    prompt = await message.answer(
        f"Получено фото: {len(items)}. Куда загрузить?",
        reply_markup=keyboard,
    )
    session.prompt_message_id = prompt.message_id
    logger.info(
        "session %s: отправлена кнопка выбора альбома (%d фото)",
        session.session_id,
        len(items),
    )


@router.message(F.video | F.animation | F.audio | F.voice | F.video_note | F.sticker)
async def on_unsupported_media(message: Message) -> None:
    await message.answer(NOT_A_PHOTO)


@router.message(F.text)
async def on_text(message: Message) -> None:
    await message.answer(NOT_A_PHOTO)


# ---------------------------------------------------------------- утилиты


def _parse_user_id(raw: str | None) -> int | None:
    if not raw:
        return None
    candidate = raw.strip().split()[0].lstrip("@")
    if not candidate.lstrip("-").isdigit():
        return None
    value = int(candidate)
    return value if value > 0 else None


def _to_photo_item(message: Message) -> PhotoItem | None:
    """Преобразует сообщение в PhotoItem, выбирая максимальное разрешение."""
    if message.photo:
        photo = message.photo[-1]
        return PhotoItem(
            file_id=photo.file_id,
            message_id=message.message_id,
            file_name=f"{photo.file_unique_id}.jpg",
            mime_type="image/jpeg",
            file_size=photo.file_size,
        )

    document = message.document
    if document is not None and (document.mime_type or "").startswith("image/"):
        extension = (document.file_name or "").rsplit(".", 1)
        suffix = extension[-1] if len(extension) == 2 else "jpg"
        return PhotoItem(
            file_id=document.file_id,
            message_id=message.message_id,
            file_name=document.file_name or f"{document.file_unique_id}.{suffix}",
            mime_type=document.mime_type or "image/jpeg",
            file_size=document.file_size,
        )

    return None
