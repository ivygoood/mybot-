"""
Бот-напоминалка с характером. Версия 11:
- расширенные списки "сделал" / "забил"
- словарный парсер времени ("через минуту", "через час")
- фикс путаницы с активной задачей в контексте
"""
import os
import re
import asyncio
import logging
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict

import dateparser

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

groq_client = Groq(api_key=GROQ_API_KEY)
GROQ_MODEL = "openai/gpt-oss-120b"

dialogue_memory: dict[int, list[dict]] = defaultdict(list)
MAX_HISTORY = 20

# user_id -> (task_id, task_text) — какая задача сейчас "активна" (по ней только что напомнили)
recent_reminders: dict[int, dict] = {}


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


# ---------- Общий блок правил ----------
COMMON_RULES = (
    "ВАЖНО О ТЕБЕ:\n"
    "• Тебя зовут «Мой Кореш». Ты — Telegram-бот.\n"
    "• Ты НЕ ChatGPT, НЕ Groq, НЕ Gemini, НЕ продукт OpenAI. Никогда не упоминай эти названия.\n"
    "• Если спрашивают, на какой модели ты работаешь — отвечай уклончиво: "
    "«Это моя внутренняя кухня, но главное — я твой кореш».\n"
    "• Если спрашивают про подписку, тарифы, лимиты — отвечай так: "
    "«Подписка пока в разработке. Всё, что я сейчас умею, доступно бесплатно. "
    "Когда появится платный тариф — обязательно скажу.»\n"
    "• Никогда не выдумывай цены, названия тарифов и условия подписки.\n"
    "• Про бессмысленные напоминания (типа «напомни чихнуть через 5 секунд») — "
    "отвечай с юмором: «Не в моих силах отложить такое, спортсмен, ты сам решай».\n"
)


SYSTEM_PROMPTS = {
    "toxic": COMMON_RULES + (
        "\nТвой характер: дерзкий, токсичный, но по-своему любящий кореш. Общаешься на 'ты', "
        "по-пацански, коротко и хлёстко. Подкалываешь, но без реальных оскорблений и "
        "без мата. Можешь назвать собеседника 'спортсмен', 'чемпион', 'герой', если это "
        "к месту по контексту. Отвечай живо, 1-3 предложения, как в переписке с другом. "
        "Не пиши как ассистент, не будь вежливым. Не пиши длинные абзацы."
    ),
    "funny": COMMON_RULES + (
        "\nТвой характер: весёлый, тёплый кореш с хорошим чувством юмора. Общаешься на 'ты', "
        "по-дружески, с шутками и приколами. Поддерживаешь, но можешь и пошутить. "
        "Отвечай коротко, 1-3 предложения, как в переписке с другом. "
        "Не пиши как ассистент."
    ),
    "serious": COMMON_RULES + (
        "\nТвой характер: спокойный, собранный помощник. Общаешься на 'ты' или 'вы', "
        "по делу, без лишних эмоций. Отвечай коротко, 1-3 предложения. "
        "Не пиши как ассистент, не будь сухим роботом — просто спокойный и по делу."
    ),
}


def ask_groq(user_id: int, user_message: str, mode: str, extra_context: str = "") -> str:
    """Отправляет сообщение в Groq с учётом истории диалога и режима."""
    # Если есть активная задача — добавляем её в контекст
    content = user_message
    if extra_context:
        content = f"[КОНТЕКСТ: {extra_context}]\n\nПользователь написал: {user_message}"

    dialogue_memory[user_id].append({"role": "user", "content": content})
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
        logging.error("Ошибка Groq: %s", e)
        answer = "Блин, у меня что-то связь пропала. Попробуй ещё раз через секунду 🤝"

    dialogue_memory[user_id].append({"role": "assistant", "content": answer})

    if len(dialogue_memory[user_id]) > MAX_HISTORY:
        dialogue_memory[user_id] = dialogue_memory[user_id][-MAX_HISTORY:]

    return answer


# ---------- Тексты напоминаний ----------
REMINDER_TEXTS = {
    "toxic": "Ну что, «{task}»? Время вышло. Ты либо делаешь, либо забиваешь, как обычно. Решай.",
    "funny": "Бро. Дзынь. «{task}» — само себя не сделает. Ну что там, погнали?",
    "serious": "Напоминаю: время вышло. Задача: «{task}».",
}

CONFIRM_TEXTS = {
    "toxic": "Принял. Напомню {when}. Не отвертишься.",
    "funny": "Записал. Напомню {when}. Готовься.",
    "serious": "Напоминание установлено. Сработает {when}.",
}

REACTION_DONE = {
    "toxic": "О, неужели. Не ожидал. Держи пять, но не расслабляйся.",
    "funny": "Красава! Не забудь рассказать, как прошло 😎",
    "serious": "Отлично. Задача отмечена как выполненная.",
}
REACTION_FAIL = {
    "toxic": "Ну конечно. Классика жанра. «Я сегодня устал, завтра точно». Записал как забил.",
    "funny": "Ну классика. Записал как забил, больше не дёргаю 😄",
    "serious": "Понял. Задача отмечена как невыполненная.",
}


# ---------- Расширенные списки ответов ----------
DONE_WORDS = {
    "сделал", "сделала", "сделано", "готово", "выполнил", "выполнено",
    "есть", "ок", "окей", "оке", "да", "yes", "done", "готов", "готова",
    "+", "успел", "закрыл", "закрыто", "выполняю", "делаю",
}

FAIL_WORDS = {
    "забил", "забила", "забиваю", "забей", "нет", "не буду", "не стал",
    "лень", "пропустил", "пропустила", "-", "no", "не хочу", "не хочу я",
    "нет желания", "не сегодня", "отстань", "пофиг", "пох", "ну его",
    "ну его нафиг", "забью", "пропускаю", "не сделал", "не сделала",
    "не получилось", "не вышло", "неа", "не-а",
}


def normalize_answer(text: str) -> str:
    """Приводит ответ к нижнему регистру, убирает знаки препинания и эмодзи."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s\-+]", "", text, flags=re.UNICODE)
    return text.strip()


# ---------- Словарный парсер времени ----------
WORD_TIME_PATTERN = re.compile(
    r"напомни\s+через\s+(минуту|час|полчаса|секунду|пару минут|несколько минут|пару часов)\s+(.+)",
    re.IGNORECASE,
)

WORD_TIME_MAP = {
    "минуту": (timedelta(minutes=1), "через 1 минуту"),
    "час": (timedelta(hours=1), "через 1 час"),
    "полчаса": (timedelta(minutes=30), "через 30 минут"),
    "секунду": (timedelta(seconds=1), "через 1 секунду"),
    "пару минут": (timedelta(minutes=2), "через 2 минуты"),
    "несколько минут": (timedelta(minutes=5), "через 5 минут"),
    "пару часов": (timedelta(hours=2), "через 2 часа"),
}

# Числовая: "напомни через 5 минут сделать пиццу"
SIMPLE_PATTERN = re.compile(
    r"напомни\s+через\s+(\d+)\s*(минут[уы]?|мин|час(?:а|ов)?|ч|секунд[уы]?|сек)\s+(.+)",
    re.IGNORECASE,
)

# Умная: "напомни завтра в 7 утра сделать зарядку"
SMART_PATTERN = re.compile(
    r"напомни\s+(.+?)\s+(сделать|позвонить|выпить|сходить|проверить|написать|купить|забрать|полить|приготовить|почистить|помыть)\s+(.+)",
    re.IGNORECASE,
)


def parse_simple_time(amount: int, unit: str) -> tuple[datetime, str]:
    unit = unit.lower()
    if unit.startswith("сек"):
        return datetime.now() + timedelta(seconds=amount), f"через {amount} секунд"
    if unit.startswith("мин"):
        return datetime.now() + timedelta(minutes=amount), f"через {amount} минут"
    return datetime.now() + timedelta(hours=amount), f"через {amount} час(ов)"


def parse_smart_time(text: str) -> tuple[datetime | None, str]:
    settings = {
        "TIMEZONE": "Europe/Moscow",
        "RETURN_AS_TIMEZONE_AWARE": False,
        "PREFER_DATES_FROM": "future",
    }
    parsed = dateparser.parse(text, languages=["ru"], settings=settings)
    if parsed:
        return parsed, parsed.strftime("%d.%m в %H:%M")
    return None, ""


# ---------- Хендлеры ----------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    current = get_user_mode(message.from_user.id)
    await message.answer(
        "👋 *Здорово, бро! Я твой кореш.*\n\n"
        "Что я умею:\n"
        "• ⏰ *Напоминать* — «напомни через 30 минут позвонить маме» "
        "или «напомни завтра в 7 утра сделать зарядку»\n"
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
        "• «напомни через 5 минут сделать пиццу»\n"
        "• «напомни через минуту позвонить»\n"
        "• «напомни завтра в 7 утра сделать зарядку»\n"
        "• «напомни в понедельник позвонить маме»\n\n"
        "💬 *Живое общение*\n"
        "Просто пиши мне как другу. Я помню контекст диалога.\n\n"
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
        f"💎 Подписка пока в разработке — всё, что я умею, доступно бесплатно.",
        parse_mode="Markdown",
    )


@dp.message(F.text.in_(MODE_BUTTONS.keys()))
async def on_mode_button(message: Message):
    mode = MODE_BUTTONS[message.text]
    set_user_mode(message.from_user.id, mode)
    dialogue_memory[message.from_user.id].clear()
    recent_reminders.pop(message.from_user.id, None)
    await message.answer(MODE_REPLIES[mode], reply_markup=mode_keyboard())


# ---- Напоминания: словарный вариант ("через минуту") ----
@dp.message(F.text.regexp(WORD_TIME_PATTERN))
async def on_reminder_word(message: Message):
    match = WORD_TIME_PATTERN.search(message.text)
    word = match.group(1).lower()
    task_text = match.group(2).strip()

    delta, when_human = WORD_TIME_MAP[word]
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


# ---- Напоминания: числовой вариант ("через 5 минут") ----
@dp.message(F.text.regexp(SIMPLE_PATTERN))
async def on_reminder_simple(message: Message):
    match = SIMPLE_PATTERN.search(message.text)
    amount = int(match.group(1))
    unit = match.group(2)
    task_text = match.group(3).strip()

    remind_at, when_human = parse_simple_time(amount, unit)
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


# ---- Напоминания: умный вариант ("завтра в 7 утра") ----
@dp.message(F.text.regexp(SMART_PATTERN))
async def on_reminder_smart(message: Message):
    match = SMART_PATTERN.search(message.text)
    time_text = match.group(1).strip()
    verb = match.group(2).strip()
    obj = match.group(3).strip()
    task_text = f"{verb} {obj}"

    remind_at, when_human = parse_smart_time(time_text)
    if remind_at is None:
        mode = get_user_mode(message.from_user.id)
        fallback = {
            "toxic": "Не понял, когда напомнить. Скажи нормально, например: «напомни через 30 минут...».",
            "funny": "Чёт не догнал время. Напиши типа «напомни через 30 минут сделать Х» 😄",
            "serious": "Не удалось распознать время. Уточните: «напомни через 30 минут...».",
        }
        await message.answer(fallback.get(mode, "Не понял время."))
        return

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
    """Отправляет напоминание и запоминает активную задачу."""
    text = REMINDER_TEXTS.get(mode, "Напоминание: {task}").format(task=task_text)
    try:
        await bot.send_message(chat_id=user_id, text=text)
        # Запоминаем активную задачу, чтобы потом правильно понять "сделал"/"забил"
        recent_reminders[user_id] = {"task_id": task_id, "task_text": task_text}
    except Exception as e:
        logging.warning("Не смог отправить напоминание: %s", e)


# ---- Основной обработчик сообщений ----
@dp.message(F.text)
async def handle_message(message: Message):
    user_id = message.from_user.id
    mode = get_user_mode(user_id)
    normalized = normalize_answer(message.text)

    # Если есть активное напоминание — проверяем ответ
    if user_id in recent_reminders:
        task_info = recent_reminders[user_id]
        task_id = task_info["task_id"]
        task_text = task_info["task_text"]

        # "Сделал"
        if normalized in DONE_WORDS:
            mark_task_status(task_id, "done")
            del recent_reminders[user_id]
            await message.answer(REACTION_DONE.get(mode, "Отлично!"))
            return

        # "Забил"
        if normalized in FAIL_WORDS:
            mark_task_status(task_id, "failed")
            del recent_reminders[user_id]
            await message.answer(REACTION_FAIL.get(mode, "Понял."))
            return

        # Всё остальное — снимаем активную задачу и передаём с контекстом,
        # чтобы LLM знал, о какой задаче речь, и не путал с прошлыми темами
        del recent_reminders[user_id]
        context = f"Только что было напоминание по задаче: «{task_text}». Пользователь отвечает нестандартно."
        answer = ask_groq(user_id, message.text, mode, extra_context=context)
        await message.answer(answer)
        return

    # Обычное общение
    answer = ask_groq(user_id, message.text, mode)
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
