"""
Бот-напоминалка с характером. Версия 9 — с /help и /settings.
"""
import os
import re
import asyncio
import logging
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from groq import Groq

from database import (
    init_db, get_user_mode, set_user_mode,
    add_task, get_pending_tasks, update_task_remind_at, mark_task_status,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")
if not GROQ_API_KEY:
    raise RuntimeError("Не задана переменная окружения GROQ_API_KEY")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Клиент Groq — актуальная бесплатная модель
groq_client = Groq(api_key=GROQ_API_KEY)
GROQ_MODEL = "openai/gpt-oss-120b"

# Память диалога
dialogue_memory: dict[int, list[dict]] = defaultdict(list)
MAX_HISTORY = 20


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


# ---------- Системные промпты для трёх режимов ----------
SYSTEM_PROMPTS = {
    "toxic": (
        "Ты — дерзкий, токсичный, но по-своему любящий кореш. Общаешься на 'ты', "
        "по-пацански, коротко и хлёстко. Подкалываешь, но без реальных оскорблений и "
        "без мата. Можешь назвать собеседника 'спортсмен', 'чемпион', 'герой', если это "
        "к месту по контексту. Отвечай живо, 1-3 предложения, как в переписке с другом. "
        "Не пиши как ассистент, не будь вежливым. Не пиши длинные абзацы."
    ),
    "funny": (
        "Ты — весёлый, тёплый кореш с хорошим чувством юмора. Общаешься на 'ты', "
        "по-дружески, с шутками и приколами. Поддерживаешь, но можешь и пошутить. "
        "Отвечай коротко, 1-3 предложения, как в переписке с другом. "
        "Не пиши как ассистент."
    ),
    "serious": (
        "Ты — спокойный, собранный помощник. Общаешься на 'ты' или 'вы', "
        "по делу, без лишних эмоций. Отвечай коротко, 1-3 предложения. "
        "Не пиши как ассистент, не будь сухим роботом — просто спокойный и по делу."
    ),
}


def ask_groq(user_id: int, user_message: str, mode: str) -> str:
    """Отправляет сообщение в Groq с учётом истории диалога и режима."""
    dialogue_memory[user_id].append({"role": "user", "content": user_message})

    history = dialogue_memory[user_id][-MAX_HISTORY:]

    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPTS["funny"])},
                *history,
            ],
            temperature=0.9,
            max_tokens=250,
        )
        answer = response.choices[0].message.content.strip()
    except Exception as e:
        # Показываем ТОЧНУЮ ошибку, чтобы понять, что чинить.
        # Потом заменим на вежливую заглушку.
        logging.error("Ошибка Groq: %s", e)
        answer = f"❌ Ошибка Groq: {type(e).__name__}: {e}"

    dialogue_memory[user_id].append({"role": "assistant", "content": answer})

    if len(dialogue_memory[user_id]) > MAX_HISTORY:
        dialogue_memory[user_id] = dialogue_memory[user_id][-MAX_HISTORY:]

    return answer


# ---------- Тексты напоминаний по режимам ----------
REMINDER_TEXTS = {
    "toxic": "Ну что, {task}? Время вышло. Ты либо делаешь, либо забиваешь, как обычно. Решай.",
    "funny": "Бро. Дзынь. «{task}» — само себя не сделает. Ну что там, погнали?",
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
    unit = unit.lower()
    if unit.startswith("мин"):
        return timedelta(minutes=amount), f"{amount} минут"
    return timedelta(hours=amount), f"{amount} час(ов)"


# ---------- Хендлеры ----------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    current = get_user_mode(message.from_user.id)
    await message.answer(
        "👋 *Здорово, бро! Я твой кореш.*\n\n"
        "Что я умею:\n"
        "• ⏰ *Напоминать* — «напомни через 30 минут позвонить маме»\n"
        "• 💬 *Болтать* — просто напиши мне что угодно, я отвечу\n"
        "• 🎭 *Менять характер* — у меня три режима настроения\n\n"
        f"Сейчас у тебя включён режим: *{current}*\n\n"
        "Выбирай стиль кнопками снизу или жми /help, чтобы узнать больше.",
        reply_markup=mode_keyboard(),
        parse_mode="Markdown",
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "🤝 *Что я умею:*\n\n"
        "⏰ *Напоминания*\n"
        "Напиши «напомни через 5 минут сделать пиццу» — я напомню.\n\n"
        "💬 *Живое общение*\n"
        "Просто пиши мне как другу. Я помню контекст диалога, "
        "так что можем болтать о чём угодно.\n\n"
        "🎭 *Три режима настроения*\n"
        "😈 *Токсичный* — дерзкий и подкалываю\n"
        "🤪 *Угарный* — весёлый и тёплый\n"
        "🧐 *Серьёзный* — спокойный и по делу\n\n"
        "Меняй режим кнопками снизу.",
        parse_mode="Markdown",
    )


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    current = get_user_mode(message.from_user.id)
    await message.answer(
        f"⚙️ *Настройки*\n\n"
        f"🎭 Текущий режим: *{current}*\n\n"
        f"Меняй режим кнопками снизу.\n\n"
        f"💎 Подписка и дополнительные фишки — скоро.",
        parse_mode="Markdown",
    )


@dp.message(F.text.in_(MODE_BUTTONS.keys()))
async def on_mode_button(message: Message):
    mode = MODE_BUTTONS[message.text]
    set_user_mode(message.from_user.id, mode)
    dialogue_memory[message.from_user.id].clear()
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
    text = REMINDER_TEXTS.get(mode, "Напоминание: {task}").format(task=task_text)
    try:
        await bot.send_message(chat_id=user_id, text=text)
    except Exception as e:
        logging.warning("Не смог отправить напоминание: %s", e)


# ---- Живое общение через Groq ----
@dp.message(F.text)
async def chat_with_groq(message: Message):
    mode = get_user_mode(message.from_user.id)
    answer = ask_groq(message.from_user.id, message.text, mode)
    await message.answer(answer)


# ---------- Восстановление задач после перезапуска ----------
async def restore_pending_tasks():
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
