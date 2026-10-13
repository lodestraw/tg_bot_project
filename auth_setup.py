"""
Одноразовая настройка доступа к Google Photos.

Что делает скрипт:
  1. читает ``client_secret.json`` (OAuth-клиент типа "Desktop app" из Google Cloud Console);
  2. запускает интерактивный OAuth 2.0 flow в браузере и запрашивает scopes
     ``photoslibrary.appendonly`` + ``photoslibrary.readonly``;
  3. извлекает GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN;
  4. дописывает (или обновляет) эти три переменные в файле ``.env``.

Запуск:  python auth_setup.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CLIENT_SECRET_FILE = BASE_DIR / "client_secret.json"
ENV_FILE = BASE_DIR / ".env"

#: Рабочий набор scopes (действует после 31.03.2025).
#:   appendonly               — загрузка файлов и создание альбомов;
#:   readonly.appcreateddata  — чтение альбомов, созданных этим приложением.
SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
]

#: Старый набор. Google удалил photoslibrary.readonly из Library API после
#: 31.03.2025 — запрос этого scope теперь завершается ошибкой 403 PERMISSION_DENIED.
#: Оставлен для проектов, которым доступ был выдан до отключения:
#:   python auth_setup.py --legacy-readonly
LEGACY_SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly",
]

MANAGED_KEYS = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")


def _fail(message: str) -> "None":
    print(f"\n[ОШИБКА] {message}\n", file=sys.stderr)
    raise SystemExit(1)


def read_client_secret() -> dict:
    """Читает client_secret.json и возвращает секцию installed/web."""
    if not CLIENT_SECRET_FILE.exists():
        _fail(
            f"не найден файл {CLIENT_SECRET_FILE.name}.\n"
            "Скачайте его в Google Cloud Console: APIs & Services -> Credentials ->\n"
            "Create credentials -> OAuth client ID -> Application type: Desktop app,\n"
            "затем положите JSON в корень проекта под именем client_secret.json."
        )

    try:
        raw = json.loads(CLIENT_SECRET_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail(f"client_secret.json не является корректным JSON: {exc}")

    section = raw.get("installed") or raw.get("web")
    if not section:
        _fail(
            "в client_secret.json нет секции 'installed' (Desktop app).\n"
            "Пересоздайте OAuth client ID с типом 'Desktop app'."
        )
    if "web" in raw and "installed" not in raw:
        print(
            "[ВНИМАНИЕ] используется OAuth-клиент типа 'Web application'. "
            "Рекомендуется тип 'Desktop app' — иначе потребуется добавить "
            "redirect URI http://localhost:<порт>/ вручную."
        )
    return section


def resolve_scopes() -> list[str]:
    """Выбирает набор scopes: по умолчанию рабочий, с --legacy-readonly — старый."""
    if "--legacy-readonly" in sys.argv:
        print(
            "[ВНИМАНИЕ] запрошен устаревший scope photoslibrary.readonly. "
            "Для большинства проектов Google вернёт ошибку."
        )
        return LEGACY_SCOPES
    return SCOPES


def run_oauth_flow() -> tuple[str, str, str]:
    """Запускает локальный OAuth-сервер и возвращает (client_id, client_secret, refresh_token)."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        _fail(
            "не установлена библиотека google-auth-oauthlib.\n"
            "Установите зависимости: pip install -r requirements.txt"
        )

    section = read_client_secret()
    scopes = resolve_scopes()

    print("=" * 72)
    print(" Авторизация Google Photos")
    print("=" * 72)
    print("Запрашиваемые права:")
    for scope in scopes:
        print(f"  - {scope}")
    print(
        "\nСейчас откроется браузер. Войдите в тот аккаунт Google, в библиотеку\n"
        "которого бот будет загружать фотографии, и подтвердите доступ.\n"
        "Если приложение в режиме 'Testing', ваш аккаунт должен быть добавлен\n"
        "в список Test users на экране OAuth consent screen.\n"
    )

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_FILE), scopes=scopes)
    try:
        credentials = flow.run_local_server(
            port=0,
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
            authorization_prompt_message=(
                "Откройте ссылку в браузере, если она не открылась автоматически:\n{url}"
            ),
            success_message=(
                "Авторизация прошла успешно. Можно закрыть эту вкладку и вернуться в терминал."
            ),
            open_browser=True,
        )
    except Exception as exc:  # noqa: BLE001 - показываем пользователю любую причину сбоя
        _fail(f"OAuth flow не завершился: {exc}")

    if not credentials.refresh_token:
        _fail(
            "Google не вернул refresh_token.\n"
            "Отзовите доступ приложению на https://myaccount.google.com/permissions\n"
            "и запустите скрипт заново (нужны access_type=offline и prompt=consent)."
        )

    client_id = credentials.client_id or section.get("client_id", "")
    client_secret = credentials.client_secret or section.get("client_secret", "")
    return client_id, client_secret, credentials.refresh_token


def update_env(values: dict[str, str]) -> None:
    """Дописывает или обновляет переменные в .env, не трогая остальные строки."""
    lines: list[str] = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    remaining = dict(values)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"

    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Google Photos (добавлено автоматически скриптом auth_setup.py)")
        for key, value in remaining.items():
            lines.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def main() -> None:
    client_id, client_secret, refresh_token = run_oauth_flow()

    update_env(
        {
            "GOOGLE_CLIENT_ID": client_id,
            "GOOGLE_CLIENT_SECRET": client_secret,
            "GOOGLE_REFRESH_TOKEN": refresh_token,
        }
    )

    print("\n" + "=" * 72)
    print(" Готово. В .env записаны переменные:")
    for key in MANAGED_KEYS:
        print(f"   {key}")
    print(f" Файл: {ENV_FILE}")
    print("=" * 72)
    print(
        "\nrefresh_token бессрочный, пока вы не отзовёте доступ в аккаунте Google\n"
        "(исключение: приложение в статусе 'Testing' — тогда токен живёт 7 дней,\n"
        "после чего нужно повторно запустить python auth_setup.py).\n"
        "\nСледующий шаг: python run.py"
    )


if __name__ == "__main__":
    main()
