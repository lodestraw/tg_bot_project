"""Белый список пользователей в SQLite (aiosqlite)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS whitelist (
    telegram_user_id INTEGER PRIMARY KEY,
    added_at         TEXT NOT NULL,
    added_by         INTEGER
);
"""


class WhitelistRepository:
    """Доступ к таблице whitelist. Администратор разрешён всегда."""

    def __init__(self, db_path: Path | str, admin_id: int) -> None:
        self._db_path = str(db_path)
        self._admin_id = admin_id

    async def init(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(_SCHEMA)
            await db.commit()
        # администратор всегда присутствует в списке
        await self.add(self._admin_id, added_by=self._admin_id)

    async def is_allowed(self, telegram_user_id: int) -> bool:
        if telegram_user_id == self._admin_id:
            return True
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT 1 FROM whitelist WHERE telegram_user_id = ?", (telegram_user_id,)
            ) as cursor:
                return await cursor.fetchone() is not None

    async def add(self, telegram_user_id: int, added_by: int | None = None) -> bool:
        """True — пользователь добавлен, False — уже был в списке."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO whitelist (telegram_user_id, added_at, added_by) "
                "VALUES (?, ?, ?)",
                (telegram_user_id, now, added_by),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def remove(self, telegram_user_id: int) -> bool:
        """True — пользователь удалён, False — его не было в списке."""
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "DELETE FROM whitelist WHERE telegram_user_id = ?", (telegram_user_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def list_all(self) -> list[tuple[int, str]]:
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT telegram_user_id, added_at FROM whitelist ORDER BY added_at, telegram_user_id"
            ) as cursor:
                rows = await cursor.fetchall()
        return [(int(row[0]), str(row[1])) for row in rows]
