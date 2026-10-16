"""
Работа с облачной базой Turso (async libsql-client).
Используем HTTP вместо WebSocket — стабильно на Render Free.
"""
import os
from datetime import datetime, timedelta
from typing import Optional
import libsql_client

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

if not TURSO_URL or not TURSO_TOKEN:
    raise RuntimeError("Не заданы TURSO_DATABASE_URL и TURSO_AUTH_TOKEN")

REF_BONUS_REFERRER = 5
REF_BONUS_NEW = 3
REF_DISCOUNT_THRESHOLD = 3
REF_DISCOUNT_PERCENT = 30


async def get_client():
    """
    Создаёт async-клиент Turso.
    scheme='https' — принудительно HTTP вместо WebSocket,
    потому что на Render Free WebSocket-соединение постоянно рвётся.
    """
    return libsql_client.create_client(
        url=TURSO_URL,
        auth_token=TURSO_TOKEN,
        scheme="https",
    )


async def init_db() -> None:
    client = await get_client()
    try:
        await client.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'funny',
            premium INTEGER NOT NULL DEFAULT 0, premium_until TEXT, last_active TEXT,
            bonus_requests INTEGER NOT NULL DEFAULT 0, ref_count INTEGER NOT NULL DEFAULT 0,
            referred_by INTEGER, started_once INTEGER NOT NULL DEFAULT 0)""")
        await client.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, text TEXT NOT NULL,
            remind_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending')""")
        await client.execute("""CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY, days INTEGER NOT NULL DEFAULT 30, uses_left INTEGER NOT NULL DEFAULT 1,
            total_uses INTEGER NOT NULL DEFAULT 1, used_by INTEGER, used_at TEXT, created_at TEXT NOT NULL)""")
        await client.execute("""CREATE TABLE IF NOT EXISTS promocode_redemptions (
            code TEXT NOT NULL, user_id INTEGER NOT NULL, redeemed_at TEXT NOT NULL,
            PRIMARY KEY (code, user_id))""")
        await client.execute("""CREATE TABLE IF NOT EXISTS user_limits (
            user_id INTEGER NOT NULL, date TEXT NOT NULL, chat_requests INTEGER NOT NULL DEFAULT 0,
            reminders_created INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, date))""")
    finally:
        await client.close()


# ---------- Вспомогательные ----------

def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ---------- Режимы ----------

async def get_user_mode(user_id: int) -> str:
    client = await get_client()
    try:
        r = await client.execute("SELECT mode FROM users WHERE user_id = ?", [user_id])
        return r.rows[0][0] if r.rows else "funny"
    finally:
        await client.close()


async def set_user_mode(user_id: int, mode: str) -> None:
    client = await get_client()
    try:
        await client.execute("""INSERT INTO users (user_id, mode) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET mode = excluded.mode""", [user_id, mode])
    finally:
        await client.close()


# ---------- Премиум ----------

async def is_premium(user_id: int) -> bool:
    client = await get_client()
    try:
        r = await client.execute("SELECT premium, premium_until FROM users WHERE user_id = ?", [user_id])
        if not r.rows:
            return False
        premium, until = r.rows[0]
        if not premium:
            return False
        if until and datetime.fromisoformat(until) < datetime.now():
            await set_premium(user_id, False)
            return False
        return True
    finally:
        await client.close()


async def set_premium(user_id: int, active: bool, days: int = 30) -> None:
    until = (datetime.now() + timedelta(days=days)).isoformat() if active else None
    client = await get_client()
    try:
        await client.execute("""INSERT INTO users (user_id, premium, premium_until) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET premium = excluded.premium, premium_until = excluded.premium_until""",
            [user_id, 1 if active else 0, until])
    finally:
        await client.close()


# ---------- Лимиты ----------

FREE_CHAT_LIMIT = 10
FREE_REMINDER_LIMIT = 2


async def get_limits(user_id: int) -> dict:
    client = await get_client()
    try:
        r = await client.execute(
            "SELECT chat_requests, reminders_created FROM user_limits WHERE user_id = ? AND date = ?",
            [user_id, _today()]
        )
        b = await client.execute("SELECT bonus_requests FROM users WHERE user_id = ?", [user_id])
        used_chat = r.rows[0][0] if r.rows else 0
        used_rem = r.rows[0][1] if r.rows else 0
        bonus = b.rows[0][0] if b.rows else 0
        return {
            "chat_requests": used_chat,
            "reminders_created": used_rem,
            "bonus_requests": bonus,
            "chat_limit": FREE_CHAT_LIMIT + bonus,
            "reminder_limit": FREE_REMINDER_LIMIT,
        }
    finally:
        await client.close()


async def increment_chat_requests(user_id: int) -> int:
    client = await get_client()
    try:
        await client.execute("""INSERT INTO user_limits (user_id, date, chat_requests) VALUES (?, ?, 1)
            ON CONFLICT(user_id, date) DO UPDATE SET chat_requests = chat_requests + 1""",
            [user_id, _today()])
        r = await client.execute(
            "SELECT chat_requests FROM user_limits WHERE user_id = ? AND date = ?",
            [user_id, _today()]
        )
        return r.rows[0][0]
    finally:
        await client.close()


async def increment_reminders(user_id: int) -> int:
    client = await get_client()
    try:
        await client.execute("""INSERT INTO user_limits (user_id, date, reminders_created) VALUES (?, ?, 1)
            ON CONFLICT(user_id, date) DO UPDATE SET reminders_created = reminders_created + 1""",
            [user_id, _today()])
        r = await client.execute(
            "SELECT reminders_created FROM user_limits WHERE user_id = ? AND date = ?",
            [user_id, _today()]
        )
        return r.rows[0][0]
    finally:
        await client.close()


async def add_bonus_requests(user_id: int, amount: int) -> None:
    client = await get_client()
    try:
        await client.execute("""INSERT INTO users (user_id, bonus_requests) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET bonus_requests = bonus_requests + ?""",
            [user_id, amount, amount])
    finally:
        await client.close()


# ---------- Активность ----------

async def update_last_active(user_id: int) -> None:
    client = await get_client()
    try:
        r = await client.execute(
            "UPDATE users SET last_active = ? WHERE user_id = ?",
            [datetime.now().isoformat(), user_id]
        )
        if r.rows_affected == 0:
            await client.execute(
                "INSERT INTO users (user_id, mode, premium, last_active) VALUES (?, 'funny', 0, ?)",
                [user_id, datetime.now().isoformat()]
            )
    finally:
        await client.close()


async def get_total_users() -> int:
    client = await get_client()
    try:
        r = await client.execute("SELECT COUNT(*) FROM users")
        return r.rows[0][0]
    finally:
        await client.close()


async def get_active_users(hours: int) -> int:
    client = await get_client()
    try:
        threshold = (datetime.now() - timedelta(hours=hours)).isoformat()
        r = await client.execute("SELECT COUNT(*) FROM users WHERE last_active >= ?", [threshold])
        return r.rows[0][0]
    finally:
        await client.close()


async def get_premium_users() -> int:
    client = await get_client()
    try:
        r = await client.execute(
            "SELECT COUNT(*) FROM users WHERE premium = 1 AND (premium_until IS NULL OR premium_until >= ?)",
            [datetime.now().isoformat()]
        )
        return r.rows[0][0]
    finally:
        await client.close()


# ---------- Рефералы ----------

async def is_new_user(user_id: int) -> bool:
    client = await get_client()
    try:
        r = await client.execute("SELECT started_once FROM users WHERE user_id = ?", [user_id])
        return not r.rows or not r.rows[0][0]
    finally:
        await client.close()


async def mark_user_started(user_id: int) -> None:
    client = await get_client()
    try:
        await client.execute("""INSERT INTO users (user_id, started_once) VALUES (?, 1)
            ON CONFLICT(user_id) DO UPDATE SET started_once = 1""", [user_id])
    finally:
        await client.close()


async def register_referral(new_user_id: int, referrer_id: int) -> tuple:
    if new_user_id == referrer_id:
        return False, "Нельзя пригласить самого себя."
    client = await get_client()
    try:
        r = await client.execute(
            "SELECT started_once, referred_by FROM users WHERE user_id = ?",
            [new_user_id]
        )
        if r.rows and r.rows[0][0]:
            return False, "Этот пользователь уже пользуется ботом."
        if r.rows and r.rows[0][1]:
            return False, "Этот пользователь уже был приглашён."
        await client.execute("""INSERT INTO users (user_id, referred_by, started_once) VALUES (?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET referred_by = excluded.referred_by""",
            [new_user_id, referrer_id])
        await client.execute("""INSERT INTO users (user_id, bonus_requests, ref_count) VALUES (?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET bonus_requests = bonus_requests + ?, ref_count = ref_count + 1""",
            [referrer_id, REF_BONUS_REFERRER, REF_BONUS_REFERRER])
        return True, f"+{REF_BONUS_REFERRER} бонусных запросов"
    finally:
        await client.close()


async def get_referral_stats(user_id: int) -> dict:
    client = await get_client()
    try:
        r = await client.execute(
            "SELECT ref_count, bonus_requests FROM users WHERE user_id = ?",
            [user_id]
        )
        if not r.rows:
            return {"ref_count": 0, "bonus_requests": 0}
        return {"ref_count": r.rows[0][0] or 0, "bonus_requests": r.rows[0][1] or 0}
    finally:
        await client.close()


async def has_referral_discount(user_id: int) -> bool:
    stats = await get_referral_stats(user_id)
    return stats["ref_count"] >= REF_DISCOUNT_THRESHOLD


# ---------- Промокоды ----------

async def create_promocode(code: str, days: int = 30, uses: int = 1) -> None:
    client = await get_client()
    try:
        await client.execute(
            "INSERT INTO promocodes (code, days, uses_left, total_uses, created_at) VALUES (?, ?, ?, ?, ?)",
            [code.upper(), days, uses, uses, datetime.now().isoformat()]
        )
    finally:
        await client.close()


async def redeem_promocode(user_id: int, code: str) -> tuple:
    code = code.upper().strip()
    client = await get_client()
    try:
        r = await client.execute("SELECT days, uses_left FROM promocodes WHERE code = ?", [code])
        if not r.rows:
            return False, "Такого промокода не существует."
        days, uses_left = r.rows[0]
        if uses_left <= 0:
            return False, "Этот промокод уже исчерпан."
        chk = await client.execute(
            "SELECT 1 FROM promocode_redemptions WHERE code = ? AND user_id = ?",
            [code, user_id]
        )
        if chk.rows:
            return False, "Ты уже использовал этот промокод."
        await client.execute("UPDATE promocodes SET uses_left = uses_left - 1 WHERE code = ?", [code])
        await client.execute(
            "INSERT INTO promocode_redemptions (code, user_id, redeemed_at) VALUES (?, ?, ?)",
            [code, user_id, datetime.now().isoformat()]
        )
        await set_premium(user_id, True, days)
        return True, f"Промокод активирован! Premium на {days} дней."
    finally:
        await client.close()


# ---------- Задачи ----------

async def add_task(user_id: int, text: str, remind_at: datetime) -> int:
    client = await get_client()
    try:
        r = await client.execute(
            "INSERT INTO tasks (user_id, text, remind_at) VALUES (?, ?, ?)",
            [user_id, text, remind_at.isoformat()]
        )
        return r.last_insert_rowid
    finally:
        await client.close()


async def get_pending_tasks(user_id: Optional[int] = None) -> list:
    client = await get_client()
    try:
        if user_id is None:
            r = await client.execute(
                "SELECT id, user_id, text, remind_at FROM tasks WHERE status = 'pending'"
            )
        else:
            r = await client.execute(
                "SELECT id, user_id, text, remind_at FROM tasks WHERE status = 'pending' AND user_id = ?",
                [user_id]
            )
        return [
            {"id": row[0], "user_id": row[1], "text": row[2], "remind_at": row[3]}
            for row in r.rows
        ]
    finally:
        await client.close()


async def get_task_by_id(task_id: int) -> Optional[dict]:
    client = await get_client()
    try:
        r = await client.execute(
            "SELECT id, user_id, text, remind_at, status FROM tasks WHERE id = ?",
            [task_id]
        )
        if not r.rows:
            return None
        row = r.rows[0]
        return {"id": row[0], "user_id": row[1], "text": row[2], "remind_at": row[3], "status": row[4]}
    finally:
        await client.close()


async def update_task_remind_at(task_id: int, remind_at: datetime) -> None:
    client = await get_client()
    try:
        await client.execute(
            "UPDATE tasks SET remind_at = ? WHERE id = ?",
            [remind_at.isoformat(), task_id]
        )
    finally:
        await client.close()


async def mark_task_status(task_id: int, status: str) -> None:
    client = await get_client()
    try:
        await client.execute("UPDATE tasks SET status = ? WHERE id = ?", [status, task_id])
    finally:
        await client.close()


async def cancel_all_user_tasks(user_id: int) -> int:
    client = await get_client()
    try:
        r = await client.execute(
            "UPDATE tasks SET status = 'cancelled' WHERE user_id = ? AND status = 'pending'",
            [user_id]
        )
        return r.rows_affected
    finally:
        await client.close()


# ---------- Статистика ----------

async def get_user_stats(user_id: int) -> dict:
    client = await get_client()
    try:
        r = await client.execute(
            "SELECT status, COUNT(*) FROM tasks WHERE user_id = ? GROUP BY status",
            [user_id]
        )
        stats = {"done": 0, "failed": 0, "cancelled": 0, "pending": 0}
        for status, count in r.rows:
            if status in stats:
                stats[status] = count
        return stats
    finally:
        await client.close()
