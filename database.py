"""
Работа с SQLite: пользователи + задачи + статистика + промокоды + лимиты + рефералы.
"""
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

DB_PATH = "bot_data.db"

# Бонусы и скидки
REF_BONUS_REFERRER = 5         # +5 запросов тому, кто привёл
REF_BONUS_NEW = 3              # +3 запроса новому
REF_DISCOUNT_THRESHOLD = 3     # с 3 друзей — скидка
REF_DISCOUNT_PERCENT = 30      # 30%


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            mode TEXT NOT NULL DEFAULT 'funny',
            premium INTEGER NOT NULL DEFAULT 0,
            premium_until TEXT,
            last_active TEXT,
            bonus_requests INTEGER NOT NULL DEFAULT 0,
            ref_count INTEGER NOT NULL DEFAULT 0,
            referred_by INTEGER,
            started_once INTEGER NOT NULL DEFAULT 0
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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY,
            days INTEGER NOT NULL DEFAULT 30,
            uses_left INTEGER NOT NULL DEFAULT 1,
            total_uses INTEGER NOT NULL DEFAULT 1,
            used_by INTEGER,
            used_at TEXT,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS promocode_redemptions (
            code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            redeemed_at TEXT NOT NULL,
            PRIMARY KEY (code, user_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_limits (
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            chat_requests INTEGER NOT NULL DEFAULT 0,
            reminders_created INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, date)
        )
    """)
    conn.commit()

    # Миграция: добавляем колонки, если их нет (для существующих БД)
    for col, table in [
        ("bonus_requests", "users"),
        ("ref_count", "users"),
        ("referred_by", "users"),
        ("started_once", "users"),
        ("uses_left", "promocodes"),
        ("total_uses", "promocodes"),
    ]:
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} INTEGER DEFAULT 0")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # колонка уже есть

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


# ---------- Премиум ----------

def is_premium(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT premium, premium_until FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return False
    premium, until = row
    if not premium:
        return False
    if until and datetime.fromisoformat(until) < datetime.now():
        set_premium(user_id, False)
        return False
    return True


def set_premium(user_id: int, active: bool, days: int = 30) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    until = (datetime.now() + timedelta(days=days)).isoformat() if active else None
    cur.execute("""
        INSERT INTO users (user_id, premium, premium_until) VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET premium = excluded.premium, premium_until = excluded.premium_until
    """, (user_id, 1 if active else 0, until))
    conn.commit()
    conn.close()


# ---------- Лимиты и бонусы ----------

FREE_CHAT_LIMIT = 10
FREE_REMINDER_LIMIT = 2


def get_today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_limits(user_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT chat_requests, reminders_created FROM user_limits WHERE user_id = ? AND date = ?",
        (user_id, get_today_str())
    )
    row = cur.fetchone()
    cur.execute("SELECT bonus_requests FROM users WHERE user_id = ?", (user_id,))
    bonus_row = cur.fetchone()
    conn.close()

    used_chat = row[0] if row else 0
    used_reminders = row[1] if row else 0
    bonus = bonus_row[0] if bonus_row else 0

    return {
        "chat_requests": used_chat,
        "reminders_created": used_reminders,
        "bonus_requests": bonus,
        "chat_limit": FREE_CHAT_LIMIT + bonus,
        "reminder_limit": FREE_REMINDER_LIMIT,
    }


def increment_chat_requests(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    today = get_today_str()
    cur.execute("""
        INSERT INTO user_limits (user_id, date, chat_requests) VALUES (?, ?, 1)
        ON CONFLICT(user_id, date) DO UPDATE SET chat_requests = chat_requests + 1
    """, (user_id, today))
    conn.commit()
    cur.execute("SELECT chat_requests FROM user_limits WHERE user_id = ? AND date = ?", (user_id, today))
    val = cur.fetchone()[0]
    conn.close()
    return val


def increment_reminders(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    today = get_today_str()
    cur.execute("""
        INSERT INTO user_limits (user_id, date, reminders_created) VALUES (?, ?, 1)
        ON CONFLICT(user_id, date) DO UPDATE SET reminders_created = reminders_created + 1
    """, (user_id, today))
    conn.commit()
    cur.execute("SELECT reminders_created FROM user_limits WHERE user_id = ? AND date = ?", (user_id, today))
    val = cur.fetchone()[0]
    conn.close()
    return val


def add_bonus_requests(user_id: int, amount: int) -> None:
    """Начисляет бонусные запросы (складываются с дневным лимитом)."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, bonus_requests) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET bonus_requests = bonus_requests + ?
    """, (user_id, amount, amount))
    conn.commit()
    conn.close()


# ---------- Активность ----------

def update_last_active(user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET last_active = ? WHERE user_id = ?",
        (datetime.now().isoformat(), user_id)
    )
    if cur.rowcount == 0:
        cur.execute("""
            INSERT INTO users (user_id, mode, premium, last_active)
            VALUES (?, 'funny', 0, ?)
        """, (user_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_total_users() -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()[0]
    conn.close()
    return total


def get_active_users(hours: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    threshold = (datetime.now() - timedelta(hours=hours)).isoformat()
    cur.execute("SELECT COUNT(*) FROM users WHERE last_active >= ?", (threshold,))
    count = cur.fetchone()[0]
    conn.close()
    return count


def get_premium_users() -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = datetime.now().isoformat()
    cur.execute(
        "SELECT COUNT(*) FROM users WHERE premium = 1 AND (premium_until IS NULL OR premium_until >= ?)",
        (now,)
    )
    count = cur.fetchone()[0]
    conn.close()
    return count


# ---------- Рефералы ----------

def is_new_user(user_id: int) -> bool:
    """True, если пользователь ещё ни разу не писал боту."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT started_once FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return not row or not row[0]


def mark_user_started(user_id: int) -> None:
    """Помечаем, что юзер уже активировался (писал боту)."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, started_once) VALUES (?, 1)
        ON CONFLICT(user_id) DO UPDATE SET started_once = 1
    """, (user_id,))
    conn.commit()
    conn.close()


def register_referral(new_user_id: int, referrer_id: int) -> tuple[bool, str]:
    """
    Обрабатывает реферала. Возвращает (успех, сообщение для реферера).
    Засчитывается ТОЛЬКО если новый юзер реально новый и referrer не он сам.
    """
    if new_user_id == referrer_id:
        return False, "Нельзя пригласить самого себя."

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Проверяем, что приглашённый ещё не в базе как активный
    cur.execute("SELECT started_once, referred_by FROM users WHERE user_id = ?", (new_user_id,))
    row = cur.fetchone()

    if row and row[0]:  # уже был в боте
        conn.close()
        return False, "Этот пользователь уже пользуется ботом."

    if row and row[1]:  # уже был привязан к кому-то
        conn.close()
        return False, "Этот пользователь уже был приглашён."

    # Записываем связь
    cur.execute("""
        INSERT INTO users (user_id, referred_by, started_once) VALUES (?, ?, 0)
        ON CONFLICT(user_id) DO UPDATE SET referred_by = excluded.referred_by
    """, (new_user_id, referrer_id))

    # Начисляем бонус рефереру
    cur.execute("""
        INSERT INTO users (user_id, bonus_requests, ref_count) VALUES (?, ?, 1)
        ON CONFLICT(user_id) DO UPDATE SET
            bonus_requests = bonus_requests + ?,
            ref_count = ref_count + 1
    """, (referrer_id, REF_BONUS_REFERRER, REF_BONUS_REFERRER))

    conn.commit()
    conn.close()
    return True, f"+{REF_BONUS_REFERRER} бонусных запросов"


def get_referral_stats(user_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT ref_count, bonus_requests FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return {"ref_count": 0, "bonus_requests": 0}
    return {"ref_count": row[0] or 0, "bonus_requests": row[1] or 0}


def has_referral_discount(user_id: int) -> bool:
    """True, если у юзера 3+ друзей → скидка на Premium."""
    stats = get_referral_stats(user_id)
    return stats["ref_count"] >= REF_DISCOUNT_THRESHOLD


# ---------- Промокоды ----------

def create_promocode(code: str, days: int = 30, uses: int = 1) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO promocodes (code, days, uses_left, total_uses, created_at) VALUES (?, ?, ?, ?, ?)",
        (code.upper(), days, uses, uses, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def redeem_promocode(user_id: int, code: str) -> tuple[bool, str]:
    code = code.upper().strip()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT days, uses_left FROM promocodes WHERE code = ?", (code,))
    row = cur.fetchone()

    if not row:
        conn.close()
        return False, "Такого промокода не существует."

    days, uses_left = row
    if uses_left <= 0:
        conn.close()
        return False, "Этот промокод уже исчерпан."

    # Проверяем, не активировал ли уже этот юзер код
    cur.execute("SELECT 1 FROM promocode_redemptions WHERE code = ? AND user_id = ?", (code, user_id))
    if cur.fetchone():
        conn.close()
        return False, "Ты уже использовал этот промокод."

    # Списываем использование
    cur.execute("UPDATE promocodes SET uses_left = uses_left - 1 WHERE code = ?", (code,))
    cur.execute(
        "INSERT INTO promocode_redemptions (code, user_id, redeemed_at) VALUES (?, ?, ?)",
        (code, user_id, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

    set_premium(user_id, True, days)
    return True, f"Промокод активирован! Premium на {days} дней."


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
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if user_id is None:
        cur.execute("SELECT id, user_id, text, remind_at FROM tasks WHERE status = 'pending'")
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
    cur.execute("SELECT id, user_id, text, remind_at, status FROM tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "user_id": row[1], "text": row[2], "remind_at": row[3], "status": row[4]}


def update_task_remind_at(task_id: int, remind_at: datetime) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET remind_at = ? WHERE id = ?", (remind_at.isoformat(), task_id))
    conn.commit()
    conn.close()


def mark_task_status(task_id: int, status: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
    conn.commit()
    conn.close()


def cancel_all_user_tasks(user_id: int) -> int:
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


# ---------- Статистика ----------

def get_user_stats(user_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT status, COUNT(*) FROM tasks WHERE user_id = ? GROUP BY status", (user_id,))
    rows = cur.fetchall()
    conn.close()
    stats = {"done": 0, "failed": 0, "cancelled": 0, "pending": 0}
    for status, count in rows:
        if status in stats:
            stats[status] = count
    return stats
