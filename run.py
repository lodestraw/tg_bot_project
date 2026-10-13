"""Единая точка запуска: Telegram-бот (long polling) + FastAPI Mini App в одном процессе.

Оба компонента обязаны жить в одном процессе: сессии загрузки хранятся в памяти
и используются и ботом, и веб-бэкендом.

Запуск:  python run.py
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError

from app import errors
from app.bot.handlers import setup_routers
from app.bot.pipeline import UploadPipeline, delete_messages_safe
from app.config import get_settings
from app.db import WhitelistRepository
from app.google_photos import GooglePhotosClient
from app.sessions import SessionStore, UploadSession
from app.web.server import create_web_app

logger = logging.getLogger("run")


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def main() -> None:
    configure_logging()
    settings = get_settings()

    if not settings.webapp_url.startswith("https://"):
        logger.warning(
            "WEBAPP_URL=%s не начинается с https:// — Telegram не откроет Mini App",
            settings.webapp_url,
        )
    if not settings.google_ready:
        logger.warning(
            "Google Photos не настроен: нет GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN. "
            "Запустите: python auth_setup.py"
        )

    whitelist = WhitelistRepository(settings.database_path, settings.admin_id)
    await whitelist.init()

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=None))
    store = SessionStore(settings.session_timeout_seconds)
    photos = GooglePhotosClient(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        refresh_token=settings.google_refresh_token,
        scopes=settings.google_scopes,
    )
    pipeline = UploadPipeline(bot=bot, store=store, photos=photos, settings=settings)

    dispatcher = Dispatcher(
        store=store,
        settings=settings,
        whitelist=whitelist,
        photos=photos,
        pipeline=pipeline,
    )
    dispatcher.include_router(setup_routers(whitelist=whitelist, settings=settings))

    async def on_session_timeout(session: UploadSession) -> None:
        """HTTP 408: альбом так и не выбрали. Фото пользователя остаются в чате."""
        if session.prompt_message_id:
            with contextlib.suppress(TelegramAPIError):
                await bot.edit_message_reply_markup(
                    chat_id=session.chat_id,
                    message_id=session.prompt_message_id,
                    reply_markup=None,
                )
        await delete_messages_safe(bot, session.chat_id, session.status_message_ids)
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(
                session.chat_id,
                f"{errors.request_timeout().as_text()}\n\n"
                "Фотографии остались в чате — пришлите их снова, чтобы повторить загрузку.",
            )

    store.start_watchdog(on_session_timeout)

    web_app = create_web_app(
        store=store, photos=photos, pipeline=pipeline, settings=settings
    )
    server = uvicorn.Server(
        uvicorn.Config(
            web_app,
            host=settings.host,
            port=settings.port,
            log_level="info",
            access_log=False,
        )
    )

    try:
        me = await bot.get_me()
    except TelegramAPIError as exc:
        logger.error("не удалось авторизоваться в Telegram: %s", exc)
        await bot.session.close()
        await photos.close()
        raise SystemExit(1) from exc

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("бот @%s запущен", me.username)
    logger.info("Mini App слушает http://%s:%s", settings.host, settings.port)
    logger.info("публичный адрес Mini App: %s", settings.webapp_url)

    server_task = asyncio.create_task(server.serve(), name="uvicorn")
    polling_task = asyncio.create_task(
        dispatcher.start_polling(
            bot,
            handle_signals=False,
            allowed_updates=dispatcher.resolve_used_update_types(),
        ),
        name="polling",
    )

    try:
        done, pending = await asyncio.wait(
            {server_task, polling_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
    finally:
        logger.info("останавливаюсь…")
        server.should_exit = True
        await store.shutdown()
        await photos.close()
        with contextlib.suppress(Exception):
            await dispatcher.storage.close()
        await bot.session.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(main())
