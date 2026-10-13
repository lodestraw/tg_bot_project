"""Асинхронный клиент Google Photos Library API (REST, без google-api-python-client).

Используется только то, что нужно шлюзу загрузки:
  * обновление access_token по refresh_token;
  * список альбомов (для Mini App);
  * создание альбома;
  * двухшаговая загрузка: /v1/uploads -> /v1/mediaItems:batchCreate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import aiohttp

from app import errors

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_ROOT = "https://photoslibrary.googleapis.com/v1"
ALBUMS_URL = f"{API_ROOT}/albums"
UPLOADS_URL = f"{API_ROOT}/uploads"
BATCH_CREATE_URL = f"{API_ROOT}/mediaItems:batchCreate"

#: Google принимает не больше 50 элементов за один batchCreate.
BATCH_LIMIT = 50

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_header_filename(name: str) -> str:
    """Имя файла для HTTP-заголовка: только ASCII, иначе Google вернёт 400.

    Расширение сохраняется, а нелатинское имя (например «Отпуск.jpg») заменяется
    на 'photo', чтобы в Google Photos не оказался файл без имени.
    """
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, "jpg"

    safe_stem = _UNSAFE_NAME.sub("_", stem).strip("._")
    safe_suffix = _UNSAFE_NAME.sub("", suffix).strip("._").lower() or "jpg"
    if not safe_stem:
        safe_stem = "photo"
    return f"{safe_stem[:100]}.{safe_suffix[:10]}"


class GooglePhotosClient:
    """Клиент с кэшем access_token. Создаётся один раз на процесс."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        scopes: str = "",
        request_timeout: float = 120.0,
        max_parallel_uploads: int = 3,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._scopes = scopes
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        self._session: aiohttp.ClientSession | None = None
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._token_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()
        self._upload_semaphore = asyncio.Semaphore(max_parallel_uploads)

    # ---------------- инфраструктура ----------------

    async def _http(self) -> aiohttp.ClientSession:
        # Блокировка нужна, чтобы параллельные загрузки не создали несколько
        # ClientSession (лишние сессии остались бы незакрытыми).
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=self._timeout)
            return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret and self._refresh_token)

    async def access_token(self) -> str:
        """Возвращает действующий access_token, обновляя его при необходимости."""
        if not self.configured:
            raise errors.internal(
                "не заданы GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN. "
                "Запустите python auth_setup.py"
            )

        async with self._token_lock:
            if self._access_token and time.monotonic() < self._expires_at - 60:
                return self._access_token

            payload = {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            }
            http = await self._http()
            try:
                async with http.post(TOKEN_URL, data=payload) as response:
                    body = await response.text()
                    status = response.status
            except aiohttp.ClientError as exc:
                raise errors.bad_gateway() from exc
            except asyncio.TimeoutError as exc:
                raise errors.bad_gateway(
                    "Таймаут при обращении к серверу авторизации Google."
                ) from exc

            if status != 200:
                if "invalid_grant" in body:
                    raise errors.internal(
                        "Google отклонил refresh_token (invalid_grant). "
                        "Повторите авторизацию: python auth_setup.py"
                    )
                if status == 429:
                    raise errors.service_unavailable()
                if status >= 500:
                    raise errors.bad_gateway()
                raise errors.internal(
                    f"не удалось обновить access_token (HTTP {status}): {_snippet(body)}"
                )

            data = _parse_json(body)
            token = data.get("access_token")
            if not token:
                raise errors.internal("ответ Google не содержит access_token")
            self._access_token = str(token)
            self._expires_at = time.monotonic() + float(data.get("expires_in", 3600))
            return self._access_token

    async def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {await self.access_token()}"}
        if extra:
            headers.update(extra)
        return headers

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        http = await self._http()
        headers = await self._headers()
        try:
            async with http.request(
                method, url, params=params, json=json_body, headers=headers
            ) as response:
                text = await response.text()
                status = response.status
        except aiohttp.ClientError as exc:
            raise errors.bad_gateway() from exc
        except asyncio.TimeoutError as exc:
            raise errors.bad_gateway("Таймаут при обращении к Google Photos.") from exc

        if status >= 400:
            raise _map_api_error(status, text)
        return _parse_json(text) if text.strip() else {}

    # ---------------- альбомы ----------------

    async def list_albums(self, *, page_limit: int = 20) -> list[dict[str, Any]]:
        """Возвращает альбомы, доступные приложению (id, title, количество элементов)."""
        albums: list[dict[str, Any]] = []
        page_token: str | None = None

        for _ in range(page_limit):
            params: dict[str, Any] = {"pageSize": 50}
            if page_token:
                params["pageToken"] = page_token
            data = await self._request("GET", ALBUMS_URL, params=params)
            for album in data.get("albums", []):
                album_id = album.get("id")
                if not album_id:
                    continue
                albums.append(
                    {
                        "id": album_id,
                        "title": album.get("title") or "Без названия",
                        "mediaItemsCount": int(album.get("mediaItemsCount") or 0),
                        "isWriteable": bool(album.get("isWriteable", False)),
                    }
                )
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        albums.sort(key=lambda item: str(item["title"]).lower())
        return albums

    async def create_album(self, title: str) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise errors.bad_request("Название нового альбома пустое.")
        if len(title) > 500:
            raise errors.bad_request("Название альбома длиннее 500 символов.")

        data = await self._request("POST", ALBUMS_URL, json_body={"album": {"title": title}})
        album_id = data.get("id")
        if not album_id:
            raise errors.internal("Google не вернул id созданного альбома")
        logger.info("создан альбом %r (%s)", title, album_id)
        return {"id": str(album_id), "title": str(data.get("title") or title)}

    # ---------------- загрузка ----------------

    async def upload_bytes(self, path: Path, *, file_name: str, mime_type: str) -> str:
        """Шаг 1: загружает сырые байты файла и возвращает uploadToken."""
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise errors.internal(f"не удалось прочитать временный файл: {exc}") from exc

        if not payload:
            raise errors.bad_request("Пустой медиафайл.")

        headers = await self._headers(
            {
                "Content-Type": "application/octet-stream",
                "X-Goog-Upload-Content-Type": mime_type or "image/jpeg",
                "X-Goog-Upload-Protocol": "raw",
                "X-Goog-Upload-File-Name": _safe_header_filename(file_name),
            }
        )
        http = await self._http()

        async with self._upload_semaphore:
            try:
                async with http.post(UPLOADS_URL, data=payload, headers=headers) as response:
                    text = (await response.text()).strip()
                    status = response.status
            except aiohttp.ClientError as exc:
                raise errors.bad_gateway() from exc
            except asyncio.TimeoutError as exc:
                raise errors.bad_gateway("Таймаут при загрузке байтов в Google Photos.") from exc

        if status >= 400:
            raise _map_api_error(status, text)
        if not text:
            raise errors.bad_gateway("Google не вернул uploadToken.")
        return text

    async def batch_create(
        self,
        upload_tokens: list[tuple[str, str]],
        *,
        album_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Шаг 2: привязывает uploadToken к альбому. Возвращает список неуспешных элементов."""
        failures: list[dict[str, Any]] = []

        for start in range(0, len(upload_tokens), BATCH_LIMIT):
            chunk = upload_tokens[start : start + BATCH_LIMIT]
            body: dict[str, Any] = {
                "newMediaItems": [
                    {
                        "simpleMediaItem": {
                            "uploadToken": token,
                            "fileName": _safe_header_filename(file_name),
                        }
                    }
                    for token, file_name in chunk
                ]
            }
            if album_id:
                body["albumId"] = album_id

            data = await self._request("POST", BATCH_CREATE_URL, json_body=body)

            for result in data.get("newMediaItemResults", []):
                status = result.get("status") or {}
                code = status.get("code")
                if code:  # 0 или отсутствие кода означают успех
                    failures.append({"code": code, "message": status.get("message", "unknown")})

        return failures


def _snippet(body: str) -> str:
    return " ".join(body.split())[:300]


def _parse_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise errors.bad_gateway("Google вернул некорректный JSON.") from exc
    if not isinstance(data, dict):
        raise errors.bad_gateway("Google вернул неожиданную структуру ответа.")
    return data


def _map_api_error(status: int, body: str) -> errors.AppError:
    """Переводит HTTP-ответ Google в AppError с подходящим кодом."""
    snippet = _snippet(body)
    lowered = snippet.lower()

    if (
        status == 429
        or "resource_exhausted" in lowered
        or "quota" in lowered
        or "rate limit" in lowered
    ):
        return errors.service_unavailable()
    if status in (401, 403):
        if "insufficient" in lowered or "scope" in lowered:
            return errors.internal(
                "недостаточно прав Google API (нужны scopes appendonly/readonly). "
                f"Повторите python auth_setup.py. Ответ: {snippet}"
            )
        return errors.internal(f"Google отказал в доступе (HTTP {status}): {snippet}")
    if status == 404:
        return errors.bad_request("Альбом не найден или недоступен приложению.")
    if status == 400:
        return errors.bad_request(f"Google отклонил запрос: {snippet}")
    if status >= 500:
        return errors.bad_gateway()
    return errors.internal(f"неожиданный ответ Google (HTTP {status}): {snippet}")
