"""
Первая версия бота. Просто отвечает на сообщения.
Проверяем, что вся связка GitHub -> Render -> Telegram работает.
"""
import os
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

# ---------- Читаем токен из переменных окружения ----------
# На Render мы зададим переменную BOT_TOKEN.
# Локально (на компьютере) она бы читалась из файла .env — но нам это пока не нужно.
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

# ---------- Инициализация ----------
logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ---------- Хендлеры (обработчики сообщений) ----------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Что бот отвечает на /start"""
    await message.answer(
        "Здорово, бро! Я твой будущий кореш-напоминалка.\n\n"
        "Пока я умею только повторять за тобой. "
        "Напиши что-нибудь — я эхну."
    )


@dp.message(F.text)
async def echo(message: Message):
    """Эхо: повторяет всё, что ты написал."""
    await message.answer(f"Ты написал: {message.text}")


# ---------- Запуск ----------
async def main():
    logging.info("Бот запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
