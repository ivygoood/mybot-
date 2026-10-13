"""
Бот-напоминалка. Версия 2 — с фейковым веб-сервером для Render Free.
"""
import os
import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

# ---------- Читаем переменные окружения ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))  # Render сам подставит нужный порт

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ---------- Фейковый веб-сервер, чтобы Render не убивал бота ----------
class HealthHandler(BaseHTTPRequestHandler):
    """Отвечает на любые GET-запросы простым 'OK'."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive")

    def log_message(self, *args):
        pass  # не засоряем логи


def run_health_server():
    """Запускает HTTP-сервер в отдельном потоке."""
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logging.info("Health-сервер запущен на порту %s", PORT)
    server.serve_forever()


# ---------- Хендлеры бота ----------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Здорово, бро! Я твой будущий кореш-напоминалка.\n\n"
        "Пока я умею только повторять за тобой. "
        "Напиши что-нибудь — я эхну."
    )


@dp.message(F.text)
async def echo(message: Message):
    await message.answer(f"Ты написал: {message.text}")


# ---------- Запуск ----------
async def main():
    # Запускаем фейковый веб-сервер в фоне (отдельный поток)
    threading.Thread(target=run_health_server, daemon=True).start()

    logging.info("Бот запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
