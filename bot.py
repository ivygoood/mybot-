"""
Бот-напоминалка. Версия 5 — с напоминаниями и режимами через кнопки.
"""
import os
import re
import asyncio
import logging
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from database import (
    init_db, get_user_mode, set_user_mode,
    add_task, get_pending_tasks, update_task_remind_at, mark_task_status,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()


# ---------- Фейковый веб-сервер для Render ----------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive")

    def log_message(self, *args):
        pass


def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logging.info("Health-сервер запущен на порту %s", PORT)
    server.serve_forever()


# ---------- Клавиатура с режимами ----------
MODE_BUTTONS = {
    "😈 Токсичный": "toxic",
    "🤪 Угарный": "funny",
    "🧐 Серьёзный": "serious",
}


def mode_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="😈 Токсичный")],
            [KeyboardButton(text="🤪 Угарный")],
            [KeyboardButton(text="🧐 Серьёзный")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


MODE_REPLIES = {
    "toxic": "😈 Ок, теперь я буду общаться с тобой по-токсичному. Давай задачу.",
    "funny": "🤪 Угарный режим включён. Погнали.",
    "serious": "🧐 Серьёзный режим активирован.",
}


# ---------- Тексты напоминаний по режимам ----------
# {task} — подставляется текст задачи
REMINDER_TEXTS = {
    "toxic": "Слышь, спортсмен. Время вышло. «{task}». Ты либо делаешь, либо я записываю тебя в список тех, кто «начнёт с понедельника».",
    "funny": "Бро. Время пришло. «{task}». Ты там уже начал или всё ещё в телефоне залипаешь?",
    "serious": "Напоминаю: время вышло. Задача: «{task}».",
}

CONFIRM_TEXTS = {
    "toxic": "Принял. Напомню через {when}. Не отвертишься.",
    "funny": "Записал. Через {when} напомню. Готовься.",
    "serious": "Напоминание установлено. Сработает через {when}.",
}


# ---------- Парсер "напомни через N минут/часов <текст>" ----------
REMIND_PATTERN = re.compile(
    r"напомни\s+через\s+(\d+)\s*(минут[уы]?|мин|час(?:а|ов)?|ч)\s+(.+)",
    re.IGNORECASE,
)


def parse_time(amount: int, unit: str) -> tuple[timedelta, str]:
    """Возвращает (дельта, человекочитаемое '1 минуту' / '2 часа')."""
    unit = unit.lower()
    if unit.startswith("мин"):
        return timedelta(minutes=amount), f"{amount} минут"
    return timedelta(hours=amount), f"{amount} час(ов)"


# ---------- Хендлеры ----------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    current = get_user_mode(message.from_user.id)
    await message.answer(
        f"Здорово, бро! Я твой кореш-напоминалка.\n"
        f"Текущий режим: *{current}*.\n\n"
        f"Как пользоваться:\n"
        f"• Тыкай кнопку снизу — меняешь стиль общения.\n"
        f"• Пиши «напомни через 5 минут сделать пиццу» — я напомню.",
        reply_markup=mode_keyboard(),
    )


@dp.message(F.text.in_(MODE_BUTTONS.keys()))
async def on_mode_button(message: Message):
    mode = MODE_BUTTONS[message.text]
    set_user_mode(message.from_user.id, mode)
    await message.answer(MODE_REPLIES[mode], reply_markup=mode_keyboard())


# ---- Напоминания ----
@dp.message(F.text.regexp(REMIND_PATTERN))
async def on_reminder(message: Message):
    match = REMIND_PATTERN.search(message.text)
    amount = int(match.group(1))
    unit = match.group(2)
    task_text = match.group(3).strip()

    delta, when_human = parse_time(amount, unit)
    remind_at = datetime.now() + delta

    task_id = add_task(message.from_user.id, task_text, remind_at)
    mode = get_user_mode(message.from_user.id)

    # Ставим задачу в планировщик
    scheduler.add_job(
        send_reminder,
        trigger=DateTrigger(run_date=remind_at),
        args=[message.from_user.id, task_id, task_text, mode],
        id=f"task_{task_id}",
        replace_existing=True,
    )

    confirm = CONFIRM_TEXTS.get(mode, "Принял.").format(when=when_human)
    await message.answer(confirm)


async def send_reminder(user_id: int, task_id: int, task_text: str, mode: str):
    """Отправляет напоминание в стиле выбранного режима."""
    text = REMINDER_TEXTS.get(mode, "Напоминание: {task}").format(task=task_text)
    try:
        await bot.send_message(chat_id=user_id, text=text)
    except Exception as e:
        logging.warning("Не смог отправить напоминание: %s", e)


# ---- Эхо для всего остального ----
@dp.message(F.text)
async def echo(message: Message):
    await message.answer(message.text)


# ---------- Восстановление задач после перезапуска ----------
async def restore_pending_tasks():
    """Если Render перезапустил бота — задачи из БД снова встают в очередь."""
    for task in get_pending_tasks():
        remind_at = datetime.fromisoformat(task["remind_at"])
        if remind_at <= datetime.now():
            remind_at = datetime.now() + timedelta(seconds=10)
        mode = get_user_mode(task["user_id"])
        scheduler.add_job(
            send_reminder,
            trigger=DateTrigger(run_date=remind_at),
            args=[task["user_id"], task["id"], task["text"], mode],
            id=f"task_{task['id']}",
            replace_existing=True,
        )
        logging.info("Восстановлена задача #%s на %s", task["id"], remind_at)


# ---------- Запуск ----------
async def main():
    init_db()
    scheduler.start()
    threading.Thread(target=run_health_server, daemon=True).start()
    await restore_pending_tasks()
    logging.info("Бот запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
