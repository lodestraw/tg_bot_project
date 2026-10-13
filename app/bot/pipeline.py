"""Конвейер загрузки: Telegram -> временный файл -> Google Photos -> зачистка чата."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tempfile
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError

from app import errors
from app.config import Settings
from app.google_photos import GooglePhotosClient
from app.sessions import PhotoItem, SessionState, SessionStore, UploadSession

logger = logging.getLogger(__name__)

#: deleteMessages принимает не более 100 id за вызов.
DELETE_CHUNK = 100


async def delete_messages_safe(bot: Bot, chat_id: int, message_ids: list[int]) -> None:
    """Удаляет сообщения пачками, молча игнорируя уже удалённые/недоступные."""
    unique_ids = sorted({mid for mid in message_ids if mid})
    for start in range(0, len(unique_ids), DELETE_CHUNK):
        chunk = unique_ids[start : start + DELETE_CHUNK]
        try:
            await bot.delete_messages(chat_id=chat_id, message_ids=chunk)
        except TelegramAPIError:
            # Пачка не удалась (например, часть сообщений старше 48 часов) — пробуем по одному.
            for message_id in chunk:
                with contextlib.suppress(TelegramAPIError):
                    await bot.delete_message(chat_id=chat_id, message_id=message_id)


class UploadPipeline:
    """Выполняет загрузку фотографий одной сессии в выбранный альбом."""

    def __init__(
        self,
        *,
        bot: Bot,
        store: SessionStore,
        photos: GooglePhotosClient,
        settings: Settings,
    ) -> None:
        self._bot = bot
        self._store = store
        self._photos = photos
        self._settings = settings

    async def run(
        self,
        session: UploadSession,
        *,
        album_id: str | None,
        new_album_title: str | None,
    ) -> None:
        """Точка входа. Сессия уже должна быть переведена в состояние PROCESSING."""
        async with session.lock:
            status_message_id: int | None = None
            temp_dir: Path | None = None
            try:
                status_message_id = await self._send_status(
                    session, f"⏳ Загружаю {len(session.items)} фото в Google Photos…"
                )

                album = await self._resolve_album(album_id, new_album_title)
                if status_message_id is not None:
                    await self._edit_status(
                        session,
                        status_message_id,
                        f"⏳ Альбом «{album['title']}»: загружаю {len(session.items)} фото…",
                    )

                temp_dir = Path(tempfile.mkdtemp(prefix="tg_gphotos_"))
                upload_tokens = await self._upload_all(session.items, temp_dir)

                failures = await self._photos.batch_create(
                    upload_tokens, album_id=album["id"]
                )
                if failures:
                    detail = "; ".join(
                        f"code={item['code']} {item['message']}" for item in failures[:3]
                    )
                    raise errors.bad_request(
                        f"Google не принял {len(failures)} из {len(upload_tokens)} файлов: {detail}"
                    )

            except BaseException as exc:  # noqa: BLE001 - любая ошибка приводится к HTTP-формату
                if isinstance(exc, asyncio.CancelledError):
                    raise
                app_error = errors.wrap_unexpected(exc)
                logger.warning(
                    "session %s: загрузка не удалась: %s", session.session_id, app_error.as_text()
                )
                await self._on_failure(session, status_message_id, app_error)
                return
            finally:
                if temp_dir is not None:
                    shutil.rmtree(temp_dir, ignore_errors=True)

            await self._on_success(session, status_message_id, album["title"])

    # ---------------- шаги ----------------

    async def _resolve_album(
        self, album_id: str | None, new_album_title: str | None
    ) -> dict[str, str]:
        if new_album_title and new_album_title.strip():
            return await self._photos.create_album(new_album_title.strip())
        if album_id:
            return {"id": album_id, "title": "выбранный альбом"}
        raise errors.bad_request("Не передан ни album_id, ни new_album_title.")

    async def _upload_all(
        self, items: list[PhotoItem], temp_dir: Path
    ) -> list[tuple[str, str]]:
        """Скачивает каждый файл из Telegram, отдаёт его в /v1/uploads и сразу удаляет."""
        tokens: list[tuple[str, str]] = []

        for index, item in enumerate(items, start=1):
            temp_path = temp_dir / f"{index:03d}_{item.file_name}"
            try:
                await self._download(item, temp_path)
                token = await self._photos.upload_bytes(
                    temp_path, file_name=item.file_name, mime_type=item.mime_type
                )
                tokens.append((token, item.file_name))
            finally:
                # Немедленная зачистка временного файла.
                with contextlib.suppress(OSError):
                    temp_path.unlink(missing_ok=True)

        if not tokens:
            raise errors.bad_request("Не удалось подготовить ни одного файла к загрузке.")
        return tokens

    async def _download(self, item: PhotoItem, destination: Path) -> None:
        try:
            await self._bot.download(item.file_id, destination=destination)
        except TelegramBadRequest as exc:
            message = str(exc).lower()
            if "too big" in message or "file is too big" in message:
                raise errors.bad_request(
                    "Файл превышает лимит Telegram Bot API (20 МБ) и не может быть скачан."
                ) from exc
            raise errors.bad_request(f"Telegram отклонил файл: {exc}") from exc
        except TelegramNetworkError as exc:
            raise errors.internal(f"сетевая ошибка при скачивании из Telegram: {exc}") from exc
        except TelegramAPIError as exc:
            raise errors.internal(f"Telegram API: {exc}") from exc
        except OSError as exc:
            raise errors.internal(f"не удалось записать временный файл: {exc}") from exc

        if not destination.exists() or destination.stat().st_size == 0:
            raise errors.bad_request("Скачанный медиафайл пуст или повреждён.")

    # ---------------- финал ----------------

    async def _on_success(
        self, session: UploadSession, status_message_id: int | None, album_title: str
    ) -> None:
        session.state = SessionState.DONE
        await self._store.drop(session.session_id)

        to_delete = list(session.photo_message_ids)
        if session.prompt_message_id:
            to_delete.append(session.prompt_message_id)
        to_delete.extend(session.status_message_ids)
        if status_message_id:
            to_delete.append(status_message_id)

        await delete_messages_safe(self._bot, session.chat_id, to_delete)
        logger.info(
            "session %s: загружено %d фото, чат очищен",
            session.session_id,
            len(session.items),
        )

        notice_seconds = self._settings.success_notice_seconds
        if notice_seconds > 0:
            with contextlib.suppress(TelegramAPIError):
                notice = await self._bot.send_message(
                    session.chat_id,
                    f"✅ {len(session.items)} фото загружено в «{album_title}».",
                )
                await asyncio.sleep(notice_seconds)
                await delete_messages_safe(self._bot, session.chat_id, [notice.message_id])

    async def _on_failure(
        self,
        session: UploadSession,
        status_message_id: int | None,
        app_error: errors.AppError,
    ) -> None:
        # Фотографии пользователя НЕ удаляем — только свои служебные сообщения.
        service_ids = list(session.status_message_ids)
        if status_message_id:
            service_ids.append(status_message_id)
        await delete_messages_safe(self._bot, session.chat_id, service_ids)
        session.status_message_ids.clear()

        with contextlib.suppress(TelegramAPIError):
            await self._bot.send_message(
                session.chat_id,
                f"{app_error.as_text()}\n\nФотографии сохранены в чате — "
                "нажмите «📂 Выбрать альбом» ещё раз, чтобы повторить попытку.",
            )

        # Возвращаем сессию в ожидание, чтобы кнопка в чате осталась рабочей.
        session.state = SessionState.FAILED
        await self._store.renew(session)

    # ---------------- служебные сообщения ----------------

    async def _send_status(self, session: UploadSession, text: str) -> int | None:
        try:
            message = await self._bot.send_message(session.chat_id, text)
        except TelegramAPIError:
            return None
        session.status_message_ids.append(message.message_id)
        return message.message_id

    async def _edit_status(self, session: UploadSession, message_id: int, text: str) -> None:
        with contextlib.suppress(TelegramAPIError):
            await self._bot.edit_message_text(
                chat_id=session.chat_id, message_id=message_id, text=text
            )
