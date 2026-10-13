"""Middleware бота: контроль доступа и группировка фотографий (debounce)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from app.config import Settings
from app.db import WhitelistRepository

logger = logging.getLogger(__name__)

DENIED_TEMPLATE = (
    "Вам отказано в доступе добавления фото. "
    "Для получения прав свяжитесь с @{admin}"
)


def is_photo_message(message: Message) -> bool:
    """Фото как photo или как image-документ (отправка 'без сжатия')."""
    if message.photo:
        return True
    document = message.document
    if document is not None and (document.mime_type or "").startswith("image/"):
        return True
    return False


class AccessMiddleware(BaseMiddleware):
    """Пропускает только пользователей из белого списка (и администратора)."""

    def __init__(self, repository: WhitelistRepository, settings: Settings) -> None:
        self._repository = repository
        self._settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.from_user is None:
            return None

        user_id = event.from_user.id
        is_admin = user_id == self._settings.admin_id
        # Администратор разрешён всегда, даже если строка в whitelist.db удалена вручную.
        if is_admin or await self._repository.is_allowed(user_id):
            data["is_admin"] = is_admin
            return await handler(event, data)

        logger.info("доступ запрещён: user_id=%s", user_id)
        await event.answer(DENIED_TEMPLATE.format(admin=self._settings.admin_mention))
        return None


@dataclass(slots=True)
class _Buffer:
    messages: list[Message] = field(default_factory=list)
    last_arrival: float = 0.0
    started_at: float = 0.0


class AlbumMiddleware(BaseMiddleware):
    """Буферизует входящие фото и отдаёт их обработчику одним списком.

    * фото одного media_group_id собираются вместе;
    * одиночные фото, присланные подряд в один чат, тоже объединяются
      (ключ буфера — chat_id), чтобы пользователь выбирал альбом один раз;
    * окно ожидания сдвигается с каждым новым фото (debounce), но не дольше
      ALBUM_DEBOUNCE_MAX_WAIT.

    В обработчик приходит ``data["album"]`` — список сообщений.
    """

    def __init__(self, latency: float = 1.5, max_wait: float = 15.0, poll: float = 0.1) -> None:
        self._latency = latency
        self._max_wait = max_wait
        self._poll = poll
        self._buffers: dict[str, _Buffer] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not is_photo_message(event):
            return await handler(event, data)

        key = (
            f"group:{event.media_group_id}"
            if event.media_group_id
            else f"chat:{event.chat.id}"
        )
        now = time.monotonic()
        buffer = self._buffers.get(key)

        if buffer is not None:
            # Уже есть «ведущее» сообщение — просто докладываем фото в буфер.
            buffer.messages.append(event)
            buffer.last_arrival = now
            return None

        buffer = _Buffer(messages=[event], last_arrival=now, started_at=now)
        self._buffers[key] = buffer

        # Ведущее сообщение ждёт, пока поток фото не затихнет.
        while True:
            await asyncio.sleep(self._poll)
            idle = time.monotonic() - buffer.last_arrival
            waited = time.monotonic() - buffer.started_at
            if idle >= self._latency or waited >= self._max_wait:
                break

        collected = self._buffers.pop(key, buffer).messages
        collected.sort(key=lambda message: message.message_id)
        data["album"] = collected
        logger.info(
            "собрана группа из %d фото (ключ %s)", len(collected), key
        )
        return await handler(event, data)
