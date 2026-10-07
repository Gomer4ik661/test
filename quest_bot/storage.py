"""Хранение прогресса игроков в SQLite (файл создаётся автоматически)."""
from __future__ import annotations

import json
import sqlite3


class Storage:
    def __init__(self, path: str = "quest.db"):
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER PRIMARY KEY,
                scene   TEXT NOT NULL,
                flags   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS endings (
                user_id   INTEGER NOT NULL,
                ending_id TEXT NOT NULL,
                PRIMARY KEY (user_id, ending_id)
            );
            """
        )

    def get(self, user_id: int) -> tuple[str, set[str]] | None:
        row = self.db.execute("SELECT scene, flags FROM players WHERE user_id = ?", (user_id,)).fetchone()
        return (row[0], set(json.loads(row[1]))) if row else None

    def save(self, user_id: int, scene: str, flags: set[str]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO players (user_id, scene, flags) VALUES (?, ?, ?)",
            (user_id, scene, json.dumps(sorted(flags))),
        )
        self.db.commit()

    def add_ending(self, user_id: int, ending_id: str) -> bool:
        """Отмечает концовку. Возвращает True, если она открыта впервые."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO endings (user_id, ending_id) VALUES (?, ?)", (user_id, ending_id)
        )
        self.db.commit()
        return cur.rowcount == 1

    def endings(self, user_id: int) -> set[str]:
        rows = self.db.execute("SELECT ending_id FROM endings WHERE user_id = ?", (user_id,))
        return {r[0] for r in rows}
