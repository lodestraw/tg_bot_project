"""Проверка подписи Telegram.WebApp.initData.

Алгоритм (документация Telegram):
    secret_key       = HMAC_SHA256(key="WebAppData", msg=bot_token)
    data_check_string= "\\n".join(sorted("key=value", кроме hash))
    hash             = HMAC_SHA256(key=secret_key, msg=data_check_string)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from app import errors


@dataclass(slots=True)
class InitData:
    user_id: int
    first_name: str
    username: str
    auth_date: int
    raw: dict[str, str]


def validate_init_data(init_data: str, *, bot_token: str, max_age_seconds: int) -> InitData:
    """Возвращает данные пользователя или поднимает AppError(401)."""
    if not init_data:
        raise errors.unauthorized("Отсутствует Telegram initData.")

    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError as exc:
        raise errors.unauthorized("initData имеет некорректный формат.") from exc

    received_hash = fields.pop("hash", "")
    if not received_hash:
        raise errors.unauthorized("В initData нет поля hash.")

    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        raise errors.unauthorized()

    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError:
        auth_date = 0

    if max_age_seconds > 0:
        age = time.time() - auth_date
        if auth_date <= 0 or age > max_age_seconds:
            raise errors.unauthorized("initData устарел, откройте Mini App заново.")

    try:
        user = json.loads(fields.get("user", "{}"))
    except ValueError as exc:
        raise errors.unauthorized("Не удалось разобрать поле user в initData.") from exc

    user_id = user.get("id")
    if not isinstance(user_id, int):
        raise errors.unauthorized("В initData нет идентификатора пользователя.")

    return InitData(
        user_id=user_id,
        first_name=str(user.get("first_name", "")),
        username=str(user.get("username", "")),
        auth_date=auth_date,
        raw=fields,
    )
