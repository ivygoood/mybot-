"""
Работа с облачной базой Turso через официальный libsql-клиент.
Все функции async — внутри оборачивают синхронный клиент через asyncio.to_thread,
чтобы не блокировать event loop aiogram.
"""
import os
import asyncio
import libsql
from datetime import datetime, timedelta
from typing import Optional

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

if not TURSO_URL or not TURSO_TOKEN:
    raise RuntimeError("Не заданы TURSO_DATABASE_URL и TURSO_AUTH_TOKEN")

REF_BONUS_REFERRER = 5
REF_BONUS_NEW = 3
REF_DISCOUNT_THRESHOLD = 3
REF_DISCOUNT_PERCENT = 30

FREE_CHAT_LIMIT = 10
FREE_REMINDER_LIMIT = 2


def _connect():
    """Синхронное подключение к Turso."""
    return libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ==================== SYNC-ФУНКЦИИ (внутренние) ====================

def _sync_init_db():
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'funny',
        premium INTEGER NOT NULL DEFAULT 0, premium_until TEXT, last_active TEXT,
        bonus_requests INTEGER NOT NULL DEFAULT 0, ref_count INTEGER NOT NULL DEFAULT 0,
        referred_by INTEGER, started_once INTEGER NOT NULL DEFAULT 0)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, text TEXT NOT NULL,
        remind_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS promocodes (
        code TEXT PRIMARY KEY, days INTEGER NOT NULL DEFAULT 30, uses_left INTEGER NOT NULL DEFAULT 1,
        total_uses INTEGER NOT NULL DEFAULT 1, used_by INTEGER, used_at TEXT, created_at TEXT NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS promocode_redemptions (
        code TEXT NOT NULL, user_id INTEGER NOT NULL, redeemed_at TEXT NOT NULL,
        PRIMARY KEY (code, user_id))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS user_limits (
        user_id INTEGER NOT NULL, date TEXT NOT NULL, chat_requests INTEGER NOT NULL DEFAULT 0,
        reminders_created INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, date))""")
    conn.commit()
    conn.close()


def _sync_get_mode(user_id):
    conn = _connect(); cur = conn.cursor()
    cur.execute("SELECT mode FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else "funny"


def _sync_set_mode(user_id, mode):
    conn = _connect(); cur = conn.cursor()
    cur.execute("""INSERT INTO users (user_id, mode) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET mode = excluded.mode""", (user_id, mode))
    conn.commit(); conn.close()


def _sync_is_premium(user_id):
    conn = _connect(); cur = conn.cursor()
    cur.execute("SELECT premium, premium_until FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return False
    premium, until = row
    if not premium:
        return False
    if until and datetime.fromisoformat(until) < datetime.now():
        _sync_set_premium(user_id, False)
        return False
    return True


def _sync_set_premium(user_id, active, days=30):
    until = (datetime.now() + timedelta(days=days)).isoformat() if active else None
    conn = _connect(); cur = conn.cursor()
    cur.execute("""INSERT INTO users (user_id, premium, premium_until) VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET premium = excluded.premium, premium_until = excluded.premium_until""",
        (user_id, 1 if active else 0, until))
    conn.commit(); conn.close()


def _sync_get_limits(user_id):
    conn = _connect(); cur = conn.cursor()
    cur.execute("SELECT chat_requests, reminders_created FROM user_limits WHERE user_id = ? AND date = ?",
        (user_id, _today()))
    row = cur.fetchone()
    cur.execute("SELECT bonus_requests FROM users WHERE user_id = ?", (user_id,))
    b = cur.fetchone()
    conn.close()
    used_chat = row[0] if row else 0
    used_rem = row[1] if row else 0
    bonus = b[0] if b else 0
    return {"chat_requests": used_chat, "reminders_created": used_rem, "bonus_requests": bonus,
            "chat_limit": FREE_CHAT_LIMIT + bonus, "reminder_limit": FREE_REMINDER_LIMIT}


def _sync_inc_chat(user_id):
    conn = _connect(); cur = conn.cursor()
    cur.execute("""INSERT INTO user_limits (user_id, date, chat_requests) VALUES (?, ?, 1)
        ON CONFLICT(user_id, date) DO UPDATE SET chat_requests = chat_requests + 1""", (user_id, _today()))
    conn.commit()
    cur.execute("SELECT chat_requests FROM user_limits WHERE user_id = ? AND date = ?", (user_id, _today()))
    v = cur.fetchone()[0]
    conn.close()
    return v


def _sync_inc_reminders(user_id):
    conn = _connect(); cur = conn.cursor()
    cur.execute("""INSERT INTO user_limits (user_id, date, reminders_created) VALUES (?, ?, 1)
        ON CONFLICT(user_id, date) DO UPDATE SET reminders_created = reminders_created + 1""", (user_id, _today()))
    conn.commit()
    cur.execute("SELECT reminders_created FROM user_limits WHERE user_id = ? AND date = ?", (user_id, _today()))
    v = cur.fetchone()[0]
    conn.close()
    return v


def _sync_add_bonus(user_id, amount):
    conn = _connect(); cur = conn.cursor()
    cur.execute("""INSERT INTO users (user_id, bonus_requests) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET bonus_requests = bonus_requests + ?""", (user_id, amount, amount))
    conn.commit(); conn.close()


def _sync_update_active(user_id):
    conn = _connect(); cur = conn.cursor()
    cur.execute("UPDATE users SET last_active = ? WHERE user_id = ?",
        (datetime.now().isoformat(), user_id))
    if cur.rowcount == 0:
        cur.execute("INSERT INTO users (user_id, mode, premium, last_active) VALUES (?, 'funny', 0, ?)",
            (user_id, datetime.now().isoformat()))
    conn.commit(); conn.close()


def _sync_total_users():
    conn = _connect(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    v = cur.fetchone()[0]; conn.close(); return v


def _sync_active_users(hours):
    conn = _connect(); cur = conn.cursor()
    threshold = (datetime.now() - timedelta(hours=hours)).isoformat()
    cur.execute("SELECT COUNT(*) FROM users WHERE last_active >= ?", (threshold,))
    v = cur.fetchone()[0]; conn.close(); return v


def _sync_premium_users():
    conn = _connect(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users WHERE premium = 1 AND (premium_until IS NULL OR premium_until >= ?)",
        (datetime.now().isoformat(),))
    v = cur.fetchone()[0]; conn.close(); return v


def _sync_is_new_user(user_id):
    conn = _connect(); cur = conn.cursor()
    cur.execute("SELECT started_once FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone(); conn.close()
    return not row or not row[0]


def _sync_mark_started(user_id):
    conn = _connect(); cur = conn.cursor()
    cur.execute("""INSERT INTO users (user_id, started_once) VALUES (?, 1)
        ON CONFLICT(user_id) DO UPDATE SET started_once = 1""", (user_id,))
    conn.commit(); conn.close()


def _sync_register_referral(new_user_id, referrer_id):
    if new_user_id == referrer_id:
        return False, "Нельзя пригласить самого себя."
    conn = _connect(); cur = conn.cursor()
    cur.execute("SELECT started_once, referred_by FROM users WHERE user_id = ?", (new_user_id,))
    row = cur.fetchone()
    if row and row[0]:
        conn.close(); return False, "Этот пользователь уже пользуется ботом."
    if row and row[1]:
        conn.close(); return False, "Этот пользователь уже был приглашён."
    cur.execute("""INSERT INTO users (user_id, referred_by, started_once) VALUES (?, ?, 0)
        ON CONFLICT(user_id) DO UPDATE SET referred_by = excluded.referred_by""", (new_user_id, referrer_id))
    cur.execute("""INSERT INTO users (user_id, bonus_requests, ref_count) VALUES (?, ?, 1)
        ON CONFLICT(user_id) DO UPDATE SET bonus_requests = bonus_requests + ?, ref_count = ref_count + 1""",
        (referrer_id, REF_BONUS_REFERRER, REF_BONUS_REFERRER))
    conn.commit(); conn.close()
    return True, f"+{REF_BONUS_REFERRER} бонусных запросов"


def _sync_ref_stats(user_id):
    conn = _connect(); cur = conn.cursor()
    cur.execute("SELECT ref_count, bonus_requests FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone(); conn.close()
    if not row:
        return {"ref_count": 0, "bonus_requests": 0}
    return {"ref_count": row[0] or 0, "bonus_requests": row[1] or 0}


def _sync_create_promo(code, days, uses):
    conn = _connect(); cur = conn.cursor()
    cur.execute("INSERT INTO promocodes (code, days, uses_left, total_uses, created_at) VALUES (?, ?, ?, ?, ?)",
        (code.upper(), days, uses, uses, datetime.now().isoformat()))
    conn.commit(); conn.close()


def _sync_redeem_promo(user_id, code):
    code = code.upper().strip()
    conn = _connect(); cur = conn.cursor()
    cur.execute("SELECT days, uses_left FROM promocodes WHERE code = ?", (code,))
    row = cur.fetchone()
    if not row:
        conn.close(); return False, "Такого промокода не существует."
    days, uses_left = row
    if uses_left <= 0:
        conn.close(); return False, "Этот промокод уже исчерпан."
    cur.execute("SELECT 1 FROM promocode_redemptions WHERE code = ? AND user_id = ?", (code, user_id))
    if cur.fetchone():
        conn.close(); return False, "Ты уже использовал этот промокод."
    cur.execute("UPDATE promocodes SET uses_left = uses_left - 1 WHERE code = ?", (code,))
    cur.execute("INSERT INTO promocode_redemptions (code, user_id, redeemed_at) VALUES (?, ?, ?)",
        (code, user_id, datetime.now().isoformat()))
    conn.commit(); conn.close()
    _sync_set_premium(user_id, True, days)
    return True, f"Промокод активирован! Premium на {days} дней."


def _sync_add_task(user_id, text, remind_at):
    conn = _connect(); cur = conn.cursor()
    cur.execute("INSERT INTO tasks (user_id, text, remind_at) VALUES (?, ?, ?)",
        (user_id, text, remind_at.isoformat()))
    task_id = cur.lastrowid
    conn.commit(); conn.close()
    return task_id


def _sync_pending_tasks(user_id=None):
    conn = _connect(); cur = conn.cursor()
    if user_id is None:
        cur.execute("SELECT id, user_id, text, remind_at FROM tasks WHERE status = 'pending'")
    else:
        cur.execute("SELECT id, user_id, text, remind_at FROM tasks WHERE status = 'pending' AND user_id = ?", (user_id,))
    rows = cur.fetchall(); conn.close()
    return [{"id": r[0], "user_id": r[1], "text": r[2], "remind_at": r[3]} for r in rows]


def _sync_update_task_time(task_id, remind_at):
    conn = _connect(); cur = conn.cursor()
    cur.execute("UPDATE tasks SET remind_at = ? WHERE id = ?", (remind_at.isoformat(), task_id))
    conn.commit(); conn.close()


def _sync_mark_status(task_id, status):
    conn = _connect(); cur = conn.cursor()
    cur.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
    conn.commit(); conn.close()


def _sync_cancel_all(user_id):
    conn = _connect(); cur = conn.cursor()
    cur.execute("UPDATE tasks SET status = 'cancelled' WHERE user_id = ? AND status = 'pending'", (user_id,))
    c = cur.rowcount
    conn.commit(); conn.close()
    return c


def _sync_user_stats(user_id):
    conn = _connect(); cur = conn.cursor()
    cur.execute("SELECT status, COUNT(*) FROM tasks WHERE user_id = ? GROUP BY status", (user_id,))
    rows = cur.fetchall(); conn.close()
    stats = {"done": 0, "failed": 0, "cancelled": 0, "pending": 0}
    for status, count in rows:
        if status in stats:
            stats[status] = count
    return stats


# ==================== ASYNC-ОБЁРТКИ ====================

async def init_db(): await asyncio.to_thread(_sync_init_db)
async def get_user_mode(uid): return await asyncio.to_thread(_sync_get_mode, uid)
async def set_user_mode(uid, m): await asyncio.to_thread(_sync_set_mode, uid, m)
async def is_premium(uid): return await asyncio.to_thread(_sync_is_premium, uid)
async def set_premium(uid, a, d=30): await asyncio.to_thread(_sync_set_premium, uid, a, d)
async def get_limits(uid): return await asyncio.to_thread(_sync_get_limits, uid)
async def increment_chat_requests(uid): return await asyncio.to_thread(_sync_inc_chat, uid)
async def increment_reminders(uid): return await asyncio.to_thread(_sync_inc_reminders, uid)
async def add_bonus_requests(uid, a): await asyncio.to_thread(_sync_add_bonus, uid, a)
async def update_last_active(uid): await asyncio.to_thread(_sync_update_active, uid)
async def get_total_users(): return await asyncio.to_thread(_sync_total_users)
async def get_active_users(h): return await asyncio.to_thread(_sync_active_users, h)
async def get_premium_users(): return await asyncio.to_thread(_sync_premium_users)
async def is_new_user(uid): return await asyncio.to_thread(_sync_is_new_user, uid)
async def mark_user_started(uid): await asyncio.to_thread(_sync_mark_started, uid)
async def register_referral(n, r): return await asyncio.to_thread(_sync_register_referral, n, r)
async def get_referral_stats(uid): return await asyncio.to_thread(_sync_ref_stats, uid)
async def has_referral_discount(uid):
    stats = await get_referral_stats(uid)
    return stats["ref_count"] >= REF_DISCOUNT_THRESHOLD
async def create_promocode(c, d, u): await asyncio.to_thread(_sync_create_promo, c, d, u)
async def redeem_promocode(uid, c): return await asyncio.to_thread(_sync_redeem_promo, uid, c)
async def add_task(uid, t, r): return await asyncio.to_thread(_sync_add_task, uid, t, r)
async def get_pending_tasks(uid=None): return await asyncio.to_thread(_sync_pending_tasks, uid)
async def update_task_remind_at(tid, r): await asyncio.to_thread(_sync_update_task_time, tid, r)
async def mark_task_status(tid, s): await asyncio.to_thread(_sync_mark_status, tid, s)
async def cancel_all_user_tasks(uid): return await asyncio.to_thread(_sync_cancel_all, uid)
async def get_user_stats(uid): return await asyncio.to_thread(_sync_user_stats, uid)


# ---------- Заглушки для совместимости ----------

async def get_task_by_id(task_id):
    """Не используется в текущей версии бота, но оставлено для совместимости."""
    return None


FREE_CHAT_LIMIT = 10
FREE_REMINDER_LIMIT = 2
