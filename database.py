"""
Работа с SQLite: пользователи + задачи-напоминания.
"""
import sqlite3
from datetime import datetime
from typing import Optional

DB_PATH = "bot_data.db"


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            mode TEXT NOT NULL DEFAULT 'funny'
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            remind_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        )
    """)
    conn.commit()
    conn.close()


# ---------- Режимы ----------

def get_user_mode(user_id: int) -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT mode FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else "funny"


def set_user_mode(user_id: int, mode: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, mode) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET mode = excluded.mode
    """, (user_id, mode))
    conn.commit()
    conn.close()


# ---------- Задачи ----------

def add_task(user_id: int, text: str, remind_at: datetime) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tasks (user_id, text, remind_at) VALUES (?, ?, ?)",
        (user_id, text, remind_at.isoformat())
    )
    task_id = cur.lastrowid
    conn.commit()
    conn.close()
    return task_id


def get_pending_tasks(user_id: Optional[int] = None) -> list[dict]:
    """Все невыполненные задачи. Если указан user_id — только его."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if user_id is None:
        cur.execute(
            "SELECT id, user_id, text, remind_at FROM tasks WHERE status = 'pending'"
        )
    else:
        cur.execute(
            "SELECT id, user_id, text, remind_at FROM tasks WHERE status = 'pending' AND user_id = ?",
            (user_id,)
        )
    rows = cur.fetchall()
    conn.close()
    return [
        {"id": r[0], "user_id": r[1], "text": r[2], "remind_at": r[3]}
        for r in rows
    ]


def get_task_by_id(task_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, text, remind_at, status FROM tasks WHERE id = ?",
        (task_id,)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0], "user_id": row[1], "text": row[2],
        "remind_at": row[3], "status": row[4]
    }


def update_task_remind_at(task_id: int, remind_at: datetime) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE tasks SET remind_at = ? WHERE id = ?",
        (remind_at.isoformat(), task_id)
    )
    conn.commit()
    conn.close()


def mark_task_status(task_id: int, status: str) -> None:
    """'done' / 'failed' / 'cancelled'."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
    conn.commit()
    conn.close()


def cancel_all_user_tasks(user_id: int) -> int:
    """Отменяет ВСЕ активные задачи пользователя. Возвращает количество."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE tasks SET status = 'cancelled' WHERE user_id = ? AND status = 'pending'",
        (user_id,)
    )
    count = cur.rowcount
    conn.commit()
    conn.close()
    return count
