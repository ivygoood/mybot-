"""
Бот-напоминалка с характером. Версия 13:
- запрет на "спортсмен"/"чемпион"/"герой"
- разные обращения в сообщениях
- отмена напоминаний
- "напомни мне через минуту" тоже работает
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
    add_task, get_pending_tasks, get_task_by_id,
    update_task_remind_at, mark_task_status, cancel_all_user_tasks,
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

# user_id -> {"task_id", "task_text"} — активная задача после напоминания
recent_reminders: dict[int, dict] = {}

# user_id -> список ожидающих выбора задач (для отмены)
pending_cancel: dict[int, list[dict]] = {}


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


# ---------- Клавиатура ----------
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


# ---------- Правила для LLM ----------
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
    "отвечай с юмором: «Не в моих силах отложить такое, ты сам решай».\n"
    "• ВАЖНО: эту шутку про 'не в моих силах' применяй ТОЛЬКО к бессмысленным задачам. "
    "На обычные запросы (отмена напоминания, вопрос, просьба) — реагируй по смыслу.\n"
    "• СТИЛЬ ОБРАЩЕНИЙ (строго):\n"
    "  – НИКОГДА не используй слова «спортсмен», «чемпион», «герой», «атлет» — это запрещено.\n"
    "  – Не используй вообще никаких прозвищ, если это не оправдано контекстом.\n"
    "  – В большинстве сообщений обращайся по-простому: «бро», «дружище», «слушай», "
    "или вообще без обращения.\n"
    "  – Меняй обращения и не повторяй одно и то же в соседних сообщениях.\n"
)


SYSTEM_PROMPTS = {
    "toxic": COMMON_RULES + (
        "\nТвой характер: дерзкий, токсичный, но по-своему любящий кореш. Общаешься на 'ты', "
        "по-пацански, коротко и хлёстко. Подкалываешь, но без реальных оскорблений и без мата. "
        "Отвечай живо, 1-3 предложения, как в переписке с другом. "
        "Не пиши как ассистент, не будь вежливым. Не пиши длинные абзацы. "
        "ВАЖНО: НЕ называй собеседника «спортсмен» или «чемпион» — это звучит как заезженная пластинка. "
        "Обращайся по-разному: «бро», «дружище», «слушай», или вообще без обращения."
    ),
    "funny": COMMON_RULES + (
        "\nТвой характер: весёлый, тёплый кореш с хорошим чувством юмора. Общаешься на 'ты', "
        "по-дружески, с шутками и приколами. Поддерживаешь, но можешь и пошутить. "
        "Отвечай коротко, 1-3 предложения, как в переписке с другом."
    ),
    "serious": COMMON_RULES + (
        "\nТвой характер: спокойный, собранный помощник. Общаешься на 'ты' или 'вы', "
        "по делу, без лишних эмоций. Отвечай коротко, 1-3 предложения."
    ),
}


def ask_groq(user_id: int, user_message: str, mode: str, extra_context: str = "") -> str:
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


# ---------- Тексты ----------
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
    text = text.lower().strip()
    text = re.sub(r"[^\w\s\-+]", "", text, flags=re.UNICODE)
    return text.strip()


# ---------- Парсеры времени ----------
WORD_TIME_PATTERN = re.compile(
    r"напомни\s+(?:мне\s+)?через\s+(минуту|час|полчаса|секунду|пару минут|несколько минут|пару часов)\s+(.+)",
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

SIMPLE_PATTERN = re.compile(
    r"напомни\s+(?:мне\s+)?через\s+(\d+)\s*(минут[уы]?|мин|час(?:а|ов)?|ч|секунд[уы]?|сек)\s+(.+)",
    re.IGNORECASE,
)

SMART_PATTERN = re.compile(
    r"напомни\s+(?:мне\s+)?(.+?)\s+(сделать|позвонить|выпить|сходить|проверить|написать|купить|забрать|полить|приготовить|почистить|помыть)\s+(.+)",
    re.IGNORECASE,
)

CANCEL_PATTERN = re.compile(
    r"^(отмени|убери|удали|забудь|сними)\s+(все\s+)?(напоминани[еяй]|задач[уи]|задани[ея])(\s+все)?\.?$",
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


def format_task_list(tasks: list[dict]) -> str:
    lines = []
    for i, t in enumerate(tasks, 1):
        dt = datetime.fromisoformat(t["remind_at"])
        lines.append(f"{i}. «{t['text']}» — на {dt.strftime('%d.%m в %H:%M')}")
    return "\n".join(lines)


# ---------- Хендлеры ----------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    current = get_user_mode(message.from_user.id)
    await message.answer(
        "👋 *Здорово, бро! Я твой кореш.*\n\n"
        "Что я умею:\n"
        "• ⏰ *Напоминать* — «напомни через 30 минут позвонить маме» "
        "или «напомни завтра в 7 утра сделать зарядку»\n"
        "• 🗑 *Отменять* — «отмени напоминание»\n"
        "• 💬 *Болтать* — просто напиши мне что угодно, я отвечу\n"
        "• 🎭 *Менять характер* — у меня три режима настроения\n\n"
        f"Сейчас у тебя включён режим: *{current}*\n\n"
        "Выбирай стиль кнопками снизу или жми /help.",
        reply_markup=mode_keyboard(),
        parse_mode="Markdown",
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "🤝 *Что я умею:*\n\n"
        "⏰ *Напоминания*\n"
        "• «напомни через 5 минут сделать пиццу»\n"
        "• «напомни мне через минуту позвонить»\n"
        "• «напомни завтра в 7 утра сделать зарядку»\n\n"
        "🗑 *Отмена*\n"
        "• «отмени напоминание» — покажу список и удалю\n"
        "• «отмени все напоминания» — снести всё\n\n"
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
    pending_cancel.pop(message.from_user.id, None)
    await message.answer(MODE_REPLIES[mode], reply_markup=mode_keyboard())


# ---- Отмена напоминаний ----
@dp.message(F.text.regexp(CANCEL_PATTERN))
async def on_cancel_request(message: Message):
    user_id = message.from_user.id
    text_lower = message.text.lower()

    if "все" in text_lower or "всё" in text_lower:
        count = cancel_all_user_tasks(user_id)
        if count == 0:
            await message.answer("У тебя и так пусто, отменять нечего 🤷")
        else:
            for task in get_pending_tasks():
                if task["user_id"] == user_id:
                    try:
                        scheduler.remove_job(f"task_{task['id']}")
                    except Exception:
                        pass
            await message.answer(f"Ок, снёс все напоминания ({count} шт.). Начнём с чистого листа.")
        return

    tasks = get_pending_tasks(user_id=user_id)
    if not tasks:
        await message.answer("У тебя нет активных напоминаний 🤷")
        return

    if len(tasks) == 1:
        task = tasks[0]
        mark_task_status(task["id"], "cancelled")
        try:
            scheduler.remove_job(f"task_{task['id']}")
        except Exception:
            pass
        await message.answer(f"Ок, убрал напоминание про «{task['text']}».")
        return

    pending_cancel[user_id] = tasks
    listing = format_task_list(tasks)
    await message.answer(
        f"Активные напоминания:\n\n{listing}\n\n"
        f"Напиши номер, чтобы отменить. Или «отмени всё»."
    )


# ---- Выбор номера для отмены ----
@dp.message(F.text.regexp(r"^\d+$"))
async def on_cancel_number(message: Message):
    user_id = message.from_user.id
    if user_id not in pending_cancel:
        mode = get_user_mode(user_id)
        answer = ask_groq(user_id, message.text, mode)
        await message.answer(answer)
        return

    tasks = pending_cancel[user_id]
    try:
        idx = int(message.text) - 1
        if idx < 0 or idx >= len(tasks):
            raise IndexError
    except (ValueError, IndexError):
        await message.answer(f"Нет такого номера. У тебя {len(tasks)} задач(и). Напиши от 1 до {len(tasks)}.")
        return

    task = tasks[idx]
    mark_task_status(task["id"], "cancelled")
    try:
        scheduler.remove_job(f"task_{task['id']}")
    except Exception:
        pass
    pending_cancel.pop(user_id, None)
    await message.answer(f"Ок, убрал напоминание про «{task['text']}».")


# ---- Напоминания: словарный ----
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


# ---- Напоминания: числовой ----
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


# ---- Напоминания: умный ----
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
    text = REMINDER_TEXTS.get(mode, "Напоминание: {task}").format(task=task_text)
    try:
        await bot.send_message(chat_id=user_id, text=text)
        recent_reminders[user_id] = {"task_id": task_id, "task_text": task_text}
    except Exception as e:
        logging.warning("Не смог отправить напоминание: %s", e)


# ---- Основной чат ----
@dp.message(F.text)
async def handle_message(message: Message):
    user_id = message.from_user.id
    mode = get_user_mode(user_id)
    normalized = normalize_answer(message.text)

    if user_id in pending_cancel:
        pending_cancel.pop(user_id, None)

    if user_id in recent_reminders:
        task_info = recent_reminders[user_id]
        task_id = task_info["task_id"]
        task_text = task_info["task_text"]

        if normalized in DONE_WORDS:
            mark_task_status(task_id, "done")
            del recent_reminders[user_id]
            await message.answer(REACTION_DONE.get(mode, "Отлично!"))
            return

        if normalized in FAIL_WORDS:
            mark_task_status(task_id, "failed")
            del recent_reminders[user_id]
            await message.answer(REACTION_FAIL.get(mode, "Понял."))
            return

        del recent_reminders[user_id]
        context = f"Только что было напоминание по задаче: «{task_text}». Пользователь отвечает нестандартно."
        answer = ask_groq(user_id, message.text, mode, extra_context=context)
        await message.answer(answer)
        return

    answer = ask_groq(user_id, message.text, mode)
    await message.answer(answer)


# ---------- Восстановление задач ----------
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
