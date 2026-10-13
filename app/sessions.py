"""Оперативное (in-memory) хранилище сессий загрузки.

Сессия живёт ровно столько, сколько нужно, чтобы пользователь выбрал альбом
в Mini App. Никакие file_id на диск не пишутся и из бота наружу не отдаются.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


class SessionState(str, Enum):
    PENDING = "pending"        # ждём выбор альбома в Mini App
    PROCESSING = "processing"  # идёт загрузка в Google Photos
    DONE = "done"
    FAILED = "failed"


@dataclass(slots=True)
class PhotoItem:
    """Одно входящее фото: ссылка на файл в Telegram + id сообщения для последующей зачистки."""

    file_id: str
    message_id: int
    file_name: str = "photo.jpg"
    mime_type: str = "image/jpeg"
    file_size: int | None = None


@dataclass(slots=True)
class UploadSession:
    session_id: str
    user_id: int
    chat_id: int
    items: list[PhotoItem] = field(default_factory=list)
    prompt_message_id: int | None = None
    status_message_ids: list[int] = field(default_factory=list)
    media_group_id: str | None = None
    state: SessionState = SessionState.PENDING
    created_at: float = field(default_factory=time.monotonic)
    deadline: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def photo_message_ids(self) -> list[int]:
        return [item.message_id for item in self.items]


class SessionStore:
    """Потокобезопасное (в рамках одного event loop) хранилище сессий + watchdog для HTTP 408."""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._sessions: dict[str, UploadSession] = {}
        self._guard = asyncio.Lock()
        self._watchdog: asyncio.Task | None = None

    # ---------- CRUD ----------

    async def create(
        self,
        *,
        user_id: int,
        chat_id: int,
        items: list[PhotoItem],
        media_group_id: str | None = None,
    ) -> UploadSession:
        session = UploadSession(
            session_id=secrets.token_urlsafe(16),
            user_id=user_id,
            chat_id=chat_id,
            items=items,
            media_group_id=media_group_id,
            deadline=time.monotonic() + self._ttl,
        )
        async with self._guard:
            self._sessions[session.session_id] = session
        logger.info(
            "session %s создана: user=%s, фото=%d", session.session_id, user_id, len(items)
        )
        return session

    async def get(self, session_id: str) -> UploadSession | None:
        async with self._guard:
            return self._sessions.get(session_id)

    async def drop(self, session_id: str) -> UploadSession | None:
        async with self._guard:
            return self._sessions.pop(session_id, None)

    async def renew(self, session: UploadSession) -> None:
        """Возвращает сессию в ожидание выбора альбома (после неудачной попытки)."""
        session.state = SessionState.PENDING
        session.deadline = time.monotonic() + self._ttl
        async with self._guard:
            self._sessions[session.session_id] = session

    async def claim(self, session_id: str) -> UploadSession | None:
        """Атомарно переводит сессию PENDING -> PROCESSING.

        Защищает от двойного запуска загрузки, если Mini App отправит запрос дважды.
        Возвращает None, если сессии нет или она уже обрабатывается.
        """
        async with self._guard:
            session = self._sessions.get(session_id)
            if session is None or session.state is not SessionState.PENDING:
                return None
            session.state = SessionState.PROCESSING
            return session

    # ---------- watchdog ----------

    def start_watchdog(
        self,
        on_timeout: Callable[[UploadSession], Awaitable[None]],
        interval: float = 5.0,
    ) -> asyncio.Task:
        """Запускает фоновую задачу, которая сообщает об истёкших сессиях (HTTP 408)."""

        async def _loop() -> None:
            while True:
                try:
                    await asyncio.sleep(interval)
                    now = time.monotonic()
                    async with self._guard:
                        expired = [
                            session
                            for session in self._sessions.values()
                            if session.state is SessionState.PENDING and session.deadline <= now
                        ]
                        for session in expired:
                            self._sessions.pop(session.session_id, None)
                    for session in expired:
                        logger.info("session %s истекла по таймауту", session.session_id)
                        try:
                            await on_timeout(session)
                        except Exception:  # noqa: BLE001
                            logger.exception("ошибка в обработчике таймаута сессии")
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("сбой watchdog сессий")

        self._watchdog = asyncio.create_task(_loop(), name="session-watchdog")
        return self._watchdog

    async def shutdown(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._watchdog = None
