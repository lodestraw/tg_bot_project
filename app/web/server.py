"""FastAPI-бэкенд Telegram Mini App.

Endpoints:
    GET  /                      — страница Mini App
    GET  /healthz               — проверка живости
    GET  /api/albums            — список альбомов Google Photos (нужен initData)
    POST /api/select            — выбор/создание альбома и запуск загрузки

Политика «только запись»: ни один endpoint не отдаёт медиафайлы и не позволяет
получить что-либо из Google Photos или Telegram обратно в чат.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import errors
from app.bot.pipeline import UploadPipeline
from app.config import Settings
from app.google_photos import GooglePhotosClient
from app.sessions import SessionStore
from app.web.initdata import InitData, validate_init_data

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"


class SelectAlbumRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    album_id: str | None = Field(default=None, max_length=256)
    new_album_title: str | None = Field(default=None, max_length=500)


def create_web_app(
    *,
    store: SessionStore,
    photos: GooglePhotosClient,
    pipeline: UploadPipeline,
    settings: Settings,
) -> FastAPI:
    app = FastAPI(
        title="Telegram -> Google Photos Mini App",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ---------------- общая обработка ошибок ----------------

    @app.exception_handler(errors.AppError)
    async def app_error_handler(_: Request, exc: errors.AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status if 400 <= exc.status < 600 else 500,
            content={"error": exc.as_text(), "status": exc.status},
        )

    def authorize(init_data_header: str | None) -> InitData:
        return validate_init_data(
            init_data_header or "",
            bot_token=settings.bot_token,
            max_age_seconds=settings.initdata_max_age_seconds,
        )

    async def resolve_session(session_id: str, init_data: InitData):
        session = await store.get(session_id)
        if session is None:
            raise errors.request_timeout()
        if session.user_id != init_data.user_id:
            raise errors.forbidden("Сессия принадлежит другому пользователю.")
        return session

    # ---------------- страницы ----------------

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(
            INDEX_FILE,
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"ok": True, "google_configured": photos.configured}

    # ---------------- API ----------------

    @app.get("/api/albums")
    async def api_albums(
        session_id: str = Query(min_length=1, max_length=128),
        x_telegram_init_data: str | None = Header(default=None),
    ) -> dict[str, object]:
        init_data = authorize(x_telegram_init_data)
        session = await resolve_session(session_id, init_data)

        albums: list[dict[str, object]] = []
        albums_error: str | None = None
        try:
            albums = await photos.list_albums()
        except errors.AppError as exc:
            # Список альбомов может быть недоступен (ограничения Google API) —
            # создание нового альбома при этом продолжает работать.
            albums_error = exc.as_text()
            logger.warning("не удалось получить список альбомов: %s", exc.as_text())

        return {
            "photo_count": len(session.items),
            "albums": albums,
            "albums_error": albums_error,
        }

    @app.post("/api/select")
    async def api_select(
        payload: SelectAlbumRequest,
        x_telegram_init_data: str | None = Header(default=None),
    ) -> dict[str, object]:
        init_data = authorize(x_telegram_init_data)
        await resolve_session(payload.session_id, init_data)

        title = (payload.new_album_title or "").strip()
        album_id = (payload.album_id or "").strip()
        if not title and not album_id:
            raise errors.bad_request("Нужно выбрать альбом или указать название нового.")

        session = await store.claim(payload.session_id)
        if session is None:
            raise errors.bad_request("Эта сессия уже обрабатывается или уже завершена.")

        # Загрузка выполняется в фоне, чтобы Mini App мог сразу закрыться.
        asyncio.create_task(
            pipeline.run(session, album_id=album_id or None, new_album_title=title or None),
            name=f"upload-{session.session_id}",
        )

        return {"ok": True, "photo_count": len(session.items)}

    return app
