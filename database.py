"""
Работа с SQLite. Пока храним только режим каждого пользователя.
Позже сюда добавим задачи (напоминания).
"""
import sqlite3
from typing import Optional

DB_PATH = "bot_data.db"


def init_db() -> None:
    """Создаёт таблицу пользователей, если её ещё нет."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            mode TEXT NOT NULL DEFAULT 'funny'
        )
    """)
    conn.commit()
    conn.close()


def get_user_mode(user_id: int) -> str:
    """Возвращает режим пользователя. Если его нет — 'funny' по умолчанию."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT mode FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else "funny"


def set_user_mode(user_id: int, mode: str) -> None:
    """Сохраняет выбранный режим."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, mode) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET mode = excluded.mode
    """, (user_id, mode))
    conn.commit()
    conn.close()
