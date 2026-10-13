"""Ошибки приложения в строгом HTTP-подобном формате."""

from __future__ import annotations


class AppError(Exception):
    """Ошибка, которую можно показать пользователю как '[HTTP <code>] <reason>: <detail>'."""

    def __init__(self, status: int, reason: str, detail: str) -> None:
        self.status = status
        self.reason = reason
        self.detail = detail
        super().__init__(self.as_text())

    def as_text(self) -> str:
        return f"[HTTP {self.status}] {self.reason}: {self.detail}"


def internal(detail: str) -> AppError:
    """500 — локальный сбой (файлы, Telegram, непредвиденное исключение)."""
    return AppError(500, "Internal Server Error", detail)


def bad_gateway(detail: str = "Не удалось связаться с серверами Google Photos.") -> AppError:
    """502 — сеть/недоступность Google Photos."""
    return AppError(502, "Bad Gateway", detail)


def service_unavailable(detail: str = "Превышен лимит запросов Google API.") -> AppError:
    """503 — rate limit / квоты Google API."""
    return AppError(503, "Service Unavailable", detail)


def request_timeout(detail: str = "Время ожидания выбора альбома истекло.") -> AppError:
    """408 — пользователь не выбрал альбом за отведённое время."""
    return AppError(408, "Request Timeout", detail)


def bad_request(detail: str = "Некорректный или повреждённый медиафайл.") -> AppError:
    """400 — некорректный медиафайл или аргументы запроса."""
    return AppError(400, "Bad Request", detail)


def forbidden(detail: str) -> AppError:
    """403 — нет прав (например, не администратор)."""
    return AppError(403, "Forbidden", detail)


def not_found(detail: str) -> AppError:
    """404 — сессия не найдена."""
    return AppError(404, "Not Found", detail)


def unauthorized(detail: str = "Подпись Telegram initData недействительна.") -> AppError:
    """401 — не прошла проверка initData."""
    return AppError(401, "Unauthorized", detail)


def wrap_unexpected(exc: BaseException) -> AppError:
    """Приводит любое исключение к AppError."""
    if isinstance(exc, AppError):
        return exc
    return internal(f"{type(exc).__name__}: {exc}")
