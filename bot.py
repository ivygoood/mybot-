"""
Бот-напоминалка. Версия 4 — режимы через кнопки (ReplyKeyboard).
"""
import os
import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)

from database import init_db, get_user_mode, set_user_mode

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


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


# ---------- Клавиатура выбора режима ----------
# Текст на кнопке -> внутренний код режима
MODE_BUTTONS = {
    "😈 Токсичный": "toxic",
    "🤪 Угарный": "funny",
    "🧐 Серьёзный": "serious",
}


def mode_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура снизу экрана с тремя режимами."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="😈 Токсичный")],
            [KeyboardButton(text="🤪 Угарный")],
            [KeyboardButton(text="🧐 Серьёзный")],
        ],
        resize_keyboard=True,  # компактная клавиатура
        is_persistent=True,    # всегда висит внизу
    )


# ---------- Подтверждения при смене режима ----------
MODE_REPLIES = {
    "toxic": "😈 Ок, теперь я буду общаться с тобой по-токсичному. Давай задачу.",
    "funny": "🤪 Угарный режим включён. Погнали.",
    "serious": "🧐 Серьёзный режим активирован.",
}


# ---------- Хендлеры ----------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    current = get_user_mode(message.from_user.id)
    await message.answer(
        f"Здорово, бро! Я твой будущий кореш-напоминалка.\n"
        f"Сейчас у тебя режим: *{current}*.\n\n"
        f"Тыкай кнопку снизу, чтобы сменить стиль:",
        reply_markup=mode_keyboard(),
    )


# ---- Обработка нажатий на кнопки режима ----
# Ловим только те тексты, которые есть в MODE_BUTTONS
@dp.message(F.text.in_(MODE_BUTTONS.keys()))
async def on_mode_button(message: Message):
    mode = MODE_BUTTONS[message.text]
    set_user_mode(message.from_user.id, mode)
    await message.answer(MODE_REPLIES[mode], reply_markup=mode_keyboard())


# ---- Эхо для всего остального ----
@dp.message(F.text)
async def echo(message: Message):
    mode = get_user_mode(message.from_user.id)
    await message.answer(f"[режим: {mode}] Ты написал: {message.text}")


# ---------- Запуск ----------
async def main():
    init_db()
    threading.Thread(target=run_health_server, daemon=True).start()
    logging.info("Бот запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
