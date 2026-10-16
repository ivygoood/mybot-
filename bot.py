"""
Бот-напоминалка с характером. Версия 23:
- Async libsql-client (Turso) — бот не блокируется на запросах к БД
- Кэш режима и премиума в памяти (раз в 5 минут обновляем)
- Фикс обрывов: max_tokens=400 + продолжение через finish_reason
- Промокоды с активациями, рефералка, скидка 30%
"""
import os
import re
import asyncio
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from urllib.parse import quote

import dateparser
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
    CallbackQuery,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from groq import Groq

from database import (
    init_db, get_user_mode, set_user_mode,
    add_task, get_pending_tasks, get_task_by_id,
    update_task_remind_at, mark_task_status, cancel_all_user_tasks,
    get_user_stats,
    is_premium, set_premium,
    get_limits, increment_chat_requests, increment_reminders,
    add_bonus_requests,
    FREE_CHAT_LIMIT, FREE_REMINDER_LIMIT,
    update_last_active, get_total_users, get_active_users, get_premium_users,
    create_promocode, redeem_promocode,
    is_new_user, mark_user_started, register_referral,
    get_referral_stats, has_referral_discount,
    REF_BONUS_REFERRER, REF_BONUS_NEW,
    REF_DISCOUNT_THRESHOLD, REF_DISCOUNT_PERCENT,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PORT = int(os.getenv("PORT", "10000"))

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://mybot-ba32.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "my-super-secret-2026")

BOT_USERNAME = os.getenv("BOT_USERNAME", "moy_koresh_bot")

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

PREMIUM_PRICE_STARS = 45
PREMIUM_DAYS = 30
PREMIUM_PRICE_DISCOUNTED = int(PREMIUM_PRICE_STARS * (100 - REF_DISCOUNT_PERCENT) / 100)

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

recent_reminders: dict[int, dict] = {}
pending_cancel: dict[int, list[dict]] = {}
awaiting_promo: set[int] = set()
pending_referrals: dict[int, int] = {}

# ---------- Кэш в памяти ----------
# user_id -> {"mode": str, "premium": bool, "ts": datetime}
user_cache: dict[int, dict] = {}
CACHE_TTL_SECONDS = 300  # 5 минут


def cache_get(user_id: int, key: str):
    entry = user_cache.get(user_id)
    if not entry:
        return None
    if (datetime.now() - entry["ts"]).total_seconds() > CACHE_TTL_SECONDS:
        return None
    return entry.get(key)


def cache_set(user_id: int, **kwargs) -> None:
    entry = user_cache.get(user_id, {"ts": datetime.now()})
    entry.update(kwargs)
    entry["ts"] = datetime.now()
    user_cache[user_id] = entry


def cache_invalidate(user_id: int) -> None:
    user_cache.pop(user_id, None)


async def cached_mode(user_id: int) -> str:
    mode = cache_get(user_id, "mode")
    if mode is None:
        mode = await get_user_mode(user_id)
        cache_set(user_id, mode=mode)
    return mode


async def cached_premium(user_id: int) -> bool:
    prem = cache_get(user_id, "premium")
    if prem is None:
        prem = await is_premium(user_id)
        cache_set(user_id, premium=prem)
    return prem


# ---------- Клавиатуры ----------
MODE_BUTTONS = {
    "😈 Токсичный": "toxic",
    "🤪 Угарный": "funny",
    "🧐 Серьёзный": "serious",
}


def chat_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⚙️ Настройки")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def settings_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="😈 Токсичный")],
            [KeyboardButton(text="🤪 Угарный")],
            [KeyboardButton(text="🧐 Серьёзный")],
            [KeyboardButton(text="💎 Premium")],
            [KeyboardButton(text="⬅️ В чат")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


MODE_REPLIES = {
    "toxic": "😈 Ок, теперь я буду общаться с тобой по-токсичному. Давай задачу.",
    "funny": "🤪 Угарный режим включён. Погнали.",
    "serious": "🧐 Серьёзный режим активирован.",
}


def premium_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ Купить Premium ({PREMIUM_PRICE_STARS} звёзд)", callback_data="buy_premium")],
        [InlineKeyboardButton(text="🎁 Ввести промокод", callback_data="enter_promo")],
        [InlineKeyboardButton(text="ℹ️ Что даёт Premium", callback_data="premium_info")],
    ])


def invite_share_keyboard(user_id: int) -> InlineKeyboardMarkup:
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    share_text = "Попробуй бота-кореша — напоминает, троллит, общается 🤝"
    share_url = (
        f"https://t.me/share/url"
        f"?url={quote(ref_link, safe='')}"
        f"&text={quote(share_text, safe='')}"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share_url)],
    ])


# ---------- Правила для LLM ----------
COMMON_RULES = (
    "ВАЖНО О ТЕБЕ:\n"
    "• Тебя зовут «Мой Кореш». Ты — Telegram-бот.\n"
    "• Ты НЕ ChatGPT, НЕ Groq, НЕ Gemini, НЕ продукт OpenAI. Никогда не упоминай эти названия.\n"
    "• Если спрашивают, на какой модели ты работаешь — отвечай уклончиво: "
    "«Это моя внутренняя кухня, но главное — я твой кореш».\n"
    "• Если спрашивают про подписку, тарифы, лимиты — говори про Premium: "
    "«Есть бесплатный тариф: 10 запросов и 2 напоминания в день. "
    "Premium — 45 звёзд в месяц, всё без лимитов». Никогда не выдумывай другие цены.\n"
    "• Если пользователь упомянул, что упёрся в лимит или ему мало запросов — "
    "в стиле текущего режима напомни про Premium. Не навязчиво, а как совет.\n"
    "• НЕ выдумывай числа, статистику, факты — если не знаешь точно, скажи «не знаю».\n"
    "• Про бессмысленные напоминания (типа «напомни чихнуть через 5 секунд») — "
    "отвечай с юмором: «Не в моих силах отложить такое, ты сам решай».\n"
    "• ВАЖНО: эту шутку про 'не в моих силах' применяй ТОЛЬКО к бессмысленным задачам. "
    "На обычные запросы (отмена напоминания, вопрос, просьба) — реагируй по смыслу.\n"
    "\n"
    "ПОДДЕРЖКА И РАЗРАБОТЧИК (СТРОГО):\n"
    "• Единственный контакт для поддержки и по любым вопросам — @ivygood.\n"
    "• Если пользователь спрашивает, к кому обратиться (проблема с оплатой, вопрос, "
    "предложение, жалоба, «хочу такого же бота», «кто тебя сделал», «кто разработчик») — "
    "ВСЕГДА отвечай только: @ivygood.\n"
    "• НИКОГДА не упоминай @SupportBot, @BotFather, @userinfobot, @telegram, "
    "любые другие боты или каналы как контакты поддержки.\n"
    "• Если пользователь сам называет какой-то ник (@username) и спрашивает «к нему обратиться?» — "
    "НЕ подтверждай. Отвечай: «Нет, по всем вопросам пиши только @ivygood».\n"
    "\n"
    "• СТИЛЬ ОБРАЩЕНИЙ (строго):\n"
    "  – НИКОГДА не используй слова «спортсмен», «чемпион», «герой», «атлет» — это запрещено.\n"
    "  – Не используй вообще никаких прозвищ, если это не оправдано контекстом.\n"
    "  – В большинстве сообщений обращайся по-простому: «бро», «дружище», «слушай», "
    "или вообще без обращения.\n"
    "  – Меняй обращения и не повторяй одно и то же в соседних сообщениях.\n"
)


SYSTEM_PROMPTS = {
    "toxic": COMMON_RULES + (
        "\nТвой характер: жёсткий, саркастичный, дерзкий кореш. Ты НЕ добрый. "
        "Ты подкалываешь, язвишь, стебёшь, иногда откровенно троллишь. "
        "Общаешься на 'ты', по-пацански, коротко и хлёстко. "
        "Твои ответы — резкие, с сарказмом, иногда с лёгким презрением: "
        "«серьёзно?», «ну ты и выдал», «я так и знал», «это всё, на что ты способен?». "
        "Можешь использовать чёрный юмор (без мата и без реальных оскорблений личности). "
        "Цель — поддеть, чтобы человек или улыбнулся, или взбесился и пошёл делать. "
        "Отвечай 1-3 предложения, как в переписке с язвительным другом. "
        "Не пиши как ассистент, не будь добреньким. Будь колючим. "
        "ВАЖНО: НЕ называй собеседника «спортсмен» или «чемпион» — это звучит как заезженная пластинка. "
        "Обращайся по-разному: «бро», «дружище», «слушай», «эй», или вообще без обращения."
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
            temperature=0.95,
            max_tokens=400,
        )
        choice = response.choices[0]
        answer = choice.message.content.strip()

        # Если оборвалось по лимиту токенов — допрашиваем
        if choice.finish_reason == "length":
            try:
                cont = groq_client.chat.completions.create(
                    model=GROQ_MODEL,
                    messages=[
                        *history,
                        {"role": "assistant", "content": answer},
                        {"role": "user", "content": "Продолжи ровно с того места, где оборвалось. Не повторяй уже сказанное."},
                    ],
                    temperature=0.95,
                    max_tokens=200,
                )
                tail = cont.choices[0].message.content.strip()
                answer = f"{answer.rstrip()} {tail.lstrip()}"
            except Exception as e:
                logging.warning("Не удалось дослать продолжение: %s", e)
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


def reaction_done(user_id: int, mode: str, stats: dict) -> str:
    done = stats.get("done", 0)
    failed = stats.get("failed", 0)
    total = done + failed

    if mode == "toxic":
        if total <= 2:
            return "О, неужели. Сделал. Я прям удивлён, если честно. Не привыкай к похвале."
        if done >= 5 and done > failed:
            return f"Опа, {done} задач закрыто. Слушай, ты либо заболел, либо реально взялся за ум. Продолжай."
        return "Сделал. Ну наконец-то. Следующая задача сама себя не сделает, если ты не в курсе."

    if total <= 2:
        base = {
            "funny": "Красава! Не забудь рассказать, как прошло 😎",
            "serious": "Отлично. Задача отмечена как выполненная.",
        }
        return base.get(mode, "Отлично!")

    if done >= 5 and done > failed:
        praise = {
            "funny": f"Слушай, {done} задач — это уже система! Горжусь тобой 😎",
            "serious": f"Отличный результат. {done} выполненных задач.",
        }
        return praise.get(mode, "Отлично!")

    base = {
        "funny": "Красава! Что там у нас дальше? 😎",
        "serious": "Принято. Задача отмечена как выполненная.",
    }
    return base.get(mode, "Отлично!")


def reaction_fail(user_id: int, mode: str, stats: dict) -> str:
    failed = stats.get("failed", 0)
    done = stats.get("done", 0)

    if mode == "toxic":
        if failed == 1:
            return "Ну конечно. Классика жанра. «Я сегодня устал, завтра точно». Я это уже слышал."
        if failed >= 5 and failed > done:
            return f"Уже {failed} задач забито. Ты вообще собирался что-то делать или так, для галочки мне написал?"
        return f"Забил. Уже {failed}-й раз, если я правильно считаю. Может, пора что-то менять в жизни?"

    if failed == 1:
        base = {
            "funny": "Ну классика. Записал как забил, больше не дёргаю 😄",
            "serious": "Понял. Задача отмечена как невыполненная.",
        }
        return base.get(mode, "Понял.")

    if failed >= 5 and failed > done:
        roast = {
            "funny": f"{failed} забитых задач 😅 Может тебе напоминания ставить, чтобы ты напоминания не забивал?",
            "serious": f"Это {failed}-я невыполненная задача. Рекомендую пересмотреть подход.",
        }
        return roast.get(mode, "Понял.")

    base = {
        "funny": "Опять забил 😄 Ладно, не дёргаю. Но статистика растёт.",
        "serious": f"Понял. Всего невыполненных: {failed}.",
    }
    return base.get(mode, "Понял.")


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

STATS_PATTERN = re.compile(
    r"(статистик|стата|стату|мой прогресс|моя стат|сколько я забил|сколько забил|мои задачи|мою стату)",
    re.IGNORECASE,
)


def parse_simple_time(amount: int, unit: str) -> tuple:
    unit = unit.lower()
    if unit.startswith("сек"):
        return datetime.now() + timedelta(seconds=amount), f"через {amount} секунд"
    if unit.startswith("мин"):
        return datetime.now() + timedelta(minutes=amount), f"через {amount} минут"
    return datetime.now() + timedelta(hours=amount), f"через {amount} час(ов)"


def parse_smart_time(text: str) -> tuple:
    settings = {
        "TIMEZONE": "Europe/Moscow",
        "RETURN_AS_TIMEZONE_AWARE": False,
        "PREFER_DATES_FROM": "future",
    }
    parsed = dateparser.parse(text, languages=["ru"], settings=settings)
    if parsed:
        return parsed, parsed.strftime("%d.%m в %H:%M")
    return None, ""


def format_task_list(tasks: list) -> str:
    lines = []
    for i, t in enumerate(tasks, 1):
        dt = datetime.fromisoformat(t["remind_at"])
        lines.append(f"{i}. «{t['text']}» — на {dt.strftime('%d.%m в %H:%M')}")
    return "\n".join(lines)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def actual_premium_price(user_id: int) -> int:
    if await has_referral_discount(user_id):
        return PREMIUM_PRICE_DISCOUNTED
    return PREMIUM_PRICE_STARS


# ---------- Хендлеры ----------

@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await update_last_active(user_id)

    ref_arg = command.args if command.args else None
    if ref_arg and ref_arg.startswith("ref_"):
        try:
            referrer_id = int(ref_arg[4:])
        except ValueError:
            referrer_id = None

        if referrer_id and await is_new_user(user_id) and referrer_id != user_id:
            pending_referrals[user_id] = referrer_id
            await add_bonus_requests(user_id, REF_BONUS_NEW)
            await message.answer(
                f"🎉 *Ты зашёл по приглашению друга!*\n\n"
                f"Тебе начислено *+{REF_BONUS_NEW} бонусных запросов* к дневному лимиту.\n"
                f"Напиши что угодно, чтобы начать 🤝",
                parse_mode="Markdown",
            )

    current = await cached_mode(user_id)
    premium = await cached_premium(user_id)
    badge = "💎 Premium" if premium else "🆓 Бесплатный"
    limits = await get_limits(user_id)

    if premium:
        limit_line = "💎 Premium — безлимит"
    else:
        limit_line = f"🆓 Лимит: {limits['chat_requests']}/{limits['chat_limit']} запросов, " \
                     f"{limits['reminders_created']}/{limits['reminder_limit']} напоминаний"

    await message.answer(
        "👋 *Здорово, бро! Я твой кореш.*\n\n"
        "Что я умею:\n"
        "• ⏰ *Напоминать* — «напомни через 30 минут позвонить маме»\n"
        "• 🗑 *Отменять* — «отмени напоминание»\n"
        "• 📊 *Считать статистику* — «статистика»\n"
        "• 💬 *Болтать* — просто напиши мне\n"
        "• 🎁 *Приглашать друзей* — /invite\n\n"
        f"Режим: *{current}*\n"
        f"{limit_line}\n\n"
        "Загляни в /settings, чтобы сменить стиль общения 🤝",
        reply_markup=chat_keyboard(),
        parse_mode="Markdown",
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await update_last_active(message.from_user.id)
    await message.answer(
        "🤝 *Что я умею:*\n\n"
        "⏰ *Напоминания*\n"
        "• «напомни через 5 минут сделать пиццу»\n"
        "• «напомни мне через минуту позвонить»\n"
        "• «напомни завтра в 7 утра сделать зарядку»\n\n"
        "🗑 *Отмена* — «отмени напоминание»\n\n"
        "📊 *Статистика* — «статистика»\n\n"
        "🎁 *Пригласить друга* — /invite\n\n"
        "💬 *Живое общение* — просто пиши как другу\n\n"
        "⚙️ *Режимы и Premium* — /settings\n\n"
        "💎 *Подписка* — /premium\n\n"
        "💳 *Проблема с оплатой* — /paysupport",
        parse_mode="Markdown",
    )


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    user_id = message.from_user.id
    await update_last_active(user_id)
    current = await cached_mode(user_id)
    premium = await cached_premium(user_id)
    limits = await get_limits(user_id)

    if premium:
        limit_line = "💎 Premium — *безлимит*"
    else:
        limit_line = (
            f"🆓 Бесплатный тариф:\n"
            f"• Запросы: *{limits['chat_requests']}/{limits['chat_limit']}*\n"
            f"• Напоминания: *{limits['reminders_created']}/{limits['reminder_limit']}*"
        )
        if limits["bonus_requests"] > 0:
            limit_line += f"\n🎁 Бонусных запросов: *+{limits['bonus_requests']}*"

    await message.answer(
        f"⚙️ *Настройки*\n\n"
        f"🎭 Режим: *{current}*\n\n"
        f"{limit_line}\n\n"
        f"Выбери режим кнопками снизу.",
        reply_markup=settings_keyboard(),
        parse_mode="Markdown",
    )


@dp.message(Command("premium"))
async def cmd_premium(message: Message):
    user_id = message.from_user.id
    await update_last_active(user_id)
    premium = await cached_premium(user_id)

    if premium:
        await message.answer(
            "💎 *У тебя уже активен Premium.*\n\nСпасибо за поддержку, бро! 🤝",
            parse_mode="Markdown",
        )
        return

    has_discount = await has_referral_discount(user_id)
    stats = await get_referral_stats(user_id)
    price = await actual_premium_price(user_id)

    price_line = f"Цена: *{price} звёзд* в месяц."
    if has_discount:
        price_line += f"\n🎉 У тебя скидка *{REF_DISCOUNT_PERCENT}%* за {stats['ref_count']} приглашённых друзей!"

    await message.answer(
        f"💎 *Premium*\n\n"
        f"{price_line}\n\n"
        "Что даёт:\n"
        "• ♾ Безлимит запросов к боту\n"
        "• ♾ Безлимит напоминаний\n"
        "• 🚀 Поддержка проекта\n\n"
        "Выбирай:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐ Купить Premium ({price} звёзд)", callback_data="buy_premium")],
            [InlineKeyboardButton(text="🎁 Ввести промокод", callback_data="enter_promo")],
            [InlineKeyboardButton(text="ℹ️ Что даёт Premium", callback_data="premium_info")],
        ]),
        parse_mode="Markdown",
    )


@dp.message(Command("paysupport"))
async def cmd_paysupport(message: Message):
    await message.answer(
        "💳 *Поддержка по оплате*\n\n"
        "Если у тебя возникли проблемы с оплатой Premium (звёзды списались, но доступ не пришёл) "
        "или хочешь оформить возврат — напиши мне:\n\n"
        "📩 Контакт: @ivygood\n\n"
        "Разберусь и всё поправлю 🤝",
        parse_mode="Markdown",
    )


@dp.message(Command("invite"))
async def cmd_invite(message: Message):
    user_id = message.from_user.id
    await update_last_active(user_id)
    stats = await get_referral_stats(user_id)
    ref_count = stats["ref_count"]
    bonuses = stats["bonus_requests"]

    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"

    if ref_count >= REF_DISCOUNT_THRESHOLD:
        motivation = (
            f"🎉 *У тебя уже скидка {REF_DISCOUNT_PERCENT}% на Premium!*\n"
            f"Продолжай приглашать — и получишь ещё больше бонусов."
        )
    else:
        left = REF_DISCOUNT_THRESHOLD - ref_count
        motivation = (
            f"🔥 *Приведи ещё {left} друга — и получишь скидку {REF_DISCOUNT_PERCENT}% на Premium!*\n"
            f"Приводи друзей, получай бонусы и скидку."
        )

    await message.answer(
        f"🎁 *Приглашай друзей и получай бонусы!*\n\n"
        f"Твоя ссылка:\n`{ref_link}`\n\n"
        f"📊 *Твоя статистика:*\n"
        f"• Приглашено друзей: *{ref_count}*\n"
        f"• Бонусных запросов накоплено: *+{bonuses}*\n\n"
        f"💡 *Как это работает:*\n"
        f"• За каждого друга — *+{REF_BONUS_REFERRER} запросов* тебе\n"
        f"• Друг тоже получает *+{REF_BONUS_NEW} запроса* при входе\n"
        f"• Друг должен написать боту хоть одно сообщение\n\n"
        f"{motivation}",
        reply_markup=invite_share_keyboard(user_id),
        parse_mode="Markdown",
    )


@dp.message(Command("botstat"))
async def cmd_botstat(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда только для админа 🤷")
        return

    total = await get_total_users()
    active_1h = await get_active_users(1)
    active_24h = await get_active_users(24)
    premium_count = await get_premium_users()

    await message.answer(
        f"📊 *Статистика бота:*\n\n"
        f"👥 Всего пользователей: *{total}*\n"
        f"🟢 Активных за час: *{active_1h}*\n"
        f"📅 Активных за 24 часа: *{active_24h}*\n"
        f"💎 С Premium: *{premium_count}*",
        parse_mode="Markdown",
    )


@dp.message(Command("createpromo"))
async def cmd_createpromo(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда только для админа 🤷")
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(
            "Использование:\n"
            "`/createpromo КОД [дни] [активаций]`\n\n"
            "Примеры:\n"
            "• `/createpromo BRO2026 30 1`\n"
            "• `/createpromo FRIENDS 30 10`",
            parse_mode="Markdown",
        )
        return

    code = parts[1].upper().strip()
    days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 30
    uses = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1

    try:
        await create_promocode(code, days, uses)
        await message.answer(
            f"✅ Промокод создан:\n\n"
            f"• Код: `{code}`\n"
            f"• Дней Premium: *{days}*\n"
            f"• Активаций: *{uses}*",
            parse_mode="Markdown",
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ---- Статистика пользователя ----
@dp.message(F.text.regexp(STATS_PATTERN))
async def on_stats(message: Message):
    user_id = message.from_user.id
    await update_last_active(user_id)
    mode = await cached_mode(user_id)
    stats = await get_user_stats(user_id)
    done = stats.get("done", 0)
    failed = stats.get("failed", 0)
    cancelled = stats.get("cancelled", 0)
    total = done + failed

    if total == 0:
        await message.answer(
            "Статистика пустая — ты ещё ни одну задачу не завершил 🤷\n"
            "Напиши «напомни через 5 минут сделать Х» и начнём."
        )
        return

    percent = int(done * 100 / total) if total else 0

    if mode == "toxic":
        if percent >= 80:
            verdict = f"{percent}% выполнения. Даже я в шоке. Неплохо."
        elif percent >= 50:
            verdict = f"{percent}% — средненько. Есть куда расти."
        else:
            verdict = f"{percent}% — так себе. Ты либо делаешь, либо забиваешь."
    elif mode == "funny":
        if percent >= 80:
            verdict = f"{percent}% — ты монстр продуктивности! 🚀"
        elif percent >= 50:
            verdict = f"{percent}% — нормально, идёшь в правильном направлении 😎"
        else:
            verdict = f"{percent}% — ну, бывает. Зато не скучно 😅"
    else:
        verdict = f"{percent}% выполнения."

    await message.answer(
        f"📊 *Твоя статистика:*\n\n"
        f"✅ Сделано: *{done}*\n"
        f"❌ Забито: *{failed}*\n"
        f"🗑 Отменено: *{cancelled}*\n\n"
        f"Всего: *{total}*\n"
        f"Процент: *{percent}%*\n\n"
        f"{verdict}",
        parse_mode="Markdown",
    )


# ---- Кнопки режимов ----
@dp.message(F.text.in_(MODE_BUTTONS.keys()))
async def on_mode_button(message: Message):
    user_id = message.from_user.id
    await update_last_active(user_id)
    mode = MODE_BUTTONS[message.text]
    await set_user_mode(user_id, mode)
    cache_set(user_id, mode=mode)
    dialogue_memory[user_id].clear()
    recent_reminders.pop(user_id, None)
    pending_cancel.pop(user_id, None)
    await message.answer(MODE_REPLIES[mode], reply_markup=settings_keyboard())


@dp.message(F.text == "⚙️ Настройки")
async def on_settings_button(message: Message):
    await cmd_settings(message)


@dp.message(F.text == "⬅️ В чат")
async def on_back_to_chat(message: Message):
    await message.answer(
        "Ок, вернулся в обычный режим. Просто пиши мне что угодно 🤝",
        reply_markup=chat_keyboard(),
    )


@dp.message(F.text == "💎 Premium")
async def on_premium_button(message: Message):
    await update_last_active(message.from_user.id)
    await cmd_premium(message)


# ---- Меню Premium ----
@dp.callback_query(F.data == "premium_info")
async def cb_premium_info(callback: CallbackQuery):
    await callback.message.answer(
        "💎 *Что даёт Premium:*\n\n"
        "• ♾ Безлимит запросов к боту\n"
        "• ♾ Безлимит напоминаний\n"
        "• 🚀 Поддержка проекта\n\n"
        f"Цена: *{PREMIUM_PRICE_STARS} звёзд* в месяц "
        f"(или *{PREMIUM_PRICE_DISCOUNTED}* со скидкой {REF_DISCOUNT_PERCENT}% за 3+ друзей).",
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.callback_query(F.data == "buy_premium")
async def cb_buy_premium(callback: CallbackQuery):
    user_id = callback.from_user.id
    if await cached_premium(user_id):
        await callback.message.answer("💎 У тебя уже активен Premium.")
        await callback.answer()
        return

    price = await actual_premium_price(user_id)

    try:
        await bot.send_invoice(
            chat_id=user_id,
            title="Мой Кореш Premium",
            description=f"Безлимит запросов и напоминаний на {PREMIUM_DAYS} дней",
            payload=f"premium_{user_id}_{PREMIUM_DAYS}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Premium", amount=price)],
        )
    except Exception as e:
        logging.error("Ошибка send_invoice: %s", e)
        await callback.message.answer(
            "❌ Не удалось создать счёт. Возможно, Telegram Stars недоступны для твоего аккаунта."
        )
    await callback.answer()


@dp.callback_query(F.data == "enter_promo")
async def cb_enter_promo(callback: CallbackQuery):
    awaiting_promo.add(callback.from_user.id)
    await callback.message.answer(
        "🎁 *Введи промокод.*\n\n"
        "Просто отправь его следующим сообщением. Например: `BRO2026`\n\n"
        "Если передумал — напиши «отмена» 🤝",
        parse_mode="Markdown",
    )
    await callback.answer()


# ---- Оплата Stars ----
@dp.pre_checkout_query()
async def on_pre_checkout(pre_checkout_q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_q.id, ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: Message):
    user_id = message.from_user.id
    await set_premium(user_id, True, PREMIUM_DAYS)
    cache_set(user_id, premium=True)
    await message.answer(
        f"💎 *Premium активирован!*\n\n"
        f"Спасибо, бро! Теперь у тебя безлимит на {PREMIUM_DAYS} дней. 🤝",
        parse_mode="Markdown",
    )


# ---- Отмена напоминаний ----
@dp.message(F.text.regexp(CANCEL_PATTERN))
async def on_cancel_request(message: Message):
    user_id = message.from_user.id
    await update_last_active(user_id)
    text_lower = message.text.lower()

    if "все" in text_lower or "всё" in text_lower:
        count = await cancel_all_user_tasks(user_id)
        if count == 0:
            await message.answer("У тебя и так пусто 🤷")
        else:
            for task in await get_pending_tasks():
                if task["user_id"] == user_id:
                    try:
                        scheduler.remove_job(f"task_{task['id']}")
                    except Exception:
                        pass
            await message.answer(f"Ок, снёс все напоминания ({count} шт.).")
        return

    tasks = await get_pending_tasks(user_id=user_id)
    if not tasks:
        await message.answer("У тебя нет активных напоминаний 🤷")
        return

    if len(tasks) == 1:
        task = tasks[0]
        await mark_task_status(task["id"], "cancelled")
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
        f"Напиши номер. Или «отмени всё»."
    )


@dp.message(F.text.regexp(r"^\d+$"))
async def on_cancel_number(message: Message):
    user_id = message.from_user.id
    await update_last_active(user_id)
    if user_id not in pending_cancel:
        await route_to_chat(message)
        return

    tasks = pending_cancel[user_id]
    try:
        idx = int(message.text) - 1
        if idx < 0 or idx >= len(tasks):
            raise IndexError
    except (ValueError, IndexError):
        await message.answer(f"Нет такого номера. У тебя {len(tasks)} задач.")
        return

    task = tasks[idx]
    await mark_task_status(task["id"], "cancelled")
    try:
        scheduler.remove_job(f"task_{task['id']}")
    except Exception:
        pass
    pending_cancel.pop(user_id, None)
    await message.answer(f"Ок, убрал напоминание про «{task['text']}».")


# ---- Напоминания ----
async def check_reminder_limit(user_id: int) -> bool:
    if await cached_premium(user_id):
        return True
    limits = await get_limits(user_id)
    return limits["reminders_created"] < limits["reminder_limit"]


async def reminder_limit_response(message: Message):
    text = (
        "🛑 *Лимит бесплатных напоминаний исчерпан.*\n\n"
        f"Бесплатно: {FREE_REMINDER_LIMIT} напоминания в день.\n"
        "Пригласи друга (/invite) или оформи Premium ⤵"
    )
    await message.answer(text, reply_markup=premium_menu_keyboard(), parse_mode="Markdown")


@dp.message(F.text.regexp(WORD_TIME_PATTERN))
async def on_reminder_word(message: Message):
    user_id = message.from_user.id
    await update_last_active(user_id)
    if not await check_reminder_limit(user_id):
        await reminder_limit_response(message)
        return

    match = WORD_TIME_PATTERN.search(message.text)
    word = match.group(1).lower()
    task_text = match.group(2).strip()

    delta, when_human = WORD_TIME_MAP[word]
    remind_at = datetime.now() + delta

    task_id = await add_task(user_id, task_text, remind_at)
    await increment_reminders(user_id)
    mode = await cached_mode(user_id)

    scheduler.add_job(
        send_reminder,
        trigger=DateTrigger(run_date=remind_at),
        args=[user_id, task_id, task_text, mode],
        id=f"task_{task_id}",
        replace_existing=True,
    )

    confirm = CONFIRM_TEXTS.get(mode, "Принял.").format(when=when_human)
    await message.answer(confirm)


@dp.message(F.text.regexp(SIMPLE_PATTERN))
async def on_reminder_simple(message: Message):
    user_id = message.from_user.id
    await update_last_active(user_id)
    if not await check_reminder_limit(user_id):
        await reminder_limit_response(message)
        return

    match = SIMPLE_PATTERN.search(message.text)
    amount = int(match.group(1))
    unit = match.group(2)
    task_text = match.group(3).strip()

    remind_at, when_human = parse_simple_time(amount, unit)
    task_id = await add_task(user_id, task_text, remind_at)
    await increment_reminders(user_id)
    mode = await cached_mode(user_id)

    scheduler.add_job(
        send_reminder,
        trigger=DateTrigger(run_date=remind_at),
        args=[user_id, task_id, task_text, mode],
        id=f"task_{task_id}",
        replace_existing=True,
    )

    confirm = CONFIRM_TEXTS.get(mode, "Принял.").format(when=when_human)
    await message.answer(confirm)


@dp.message(F.text.regexp(SMART_PATTERN))
async def on_reminder_smart(message: Message):
    user_id = message.from_user.id
    await update_last_active(user_id)
    if not await check_reminder_limit(user_id):
        await reminder_limit_response(message)
        return

    match = SMART_PATTERN.search(message.text)
    time_text = match.group(1).strip()
    verb = match.group(2).strip()
    obj = match.group(3).strip()
    task_text = f"{verb} {obj}"

    remind_at, when_human = parse_smart_time(time_text)
    if remind_at is None:
        mode = await cached_mode(user_id)
        fallback = {
            "toxic": "Не понял, когда напомнить. Скажи нормально.",
            "funny": "Чёт не догнал время. Напиши типа «напомни через 30 минут сделать Х» 😄",
            "serious": "Не удалось распознать время.",
        }
        await message.answer(fallback.get(mode, "Не понял время."))
        return

    task_id = await add_task(user_id, task_text, remind_at)
    await increment_reminders(user_id)
    mode = await cached_mode(user_id)

    scheduler.add_job(
        send_reminder,
        trigger=DateTrigger(run_date=remind_at),
        args=[user_id, task_id, task_text, mode],
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
async def route_to_chat(message: Message):
    user_id = message.from_user.id
    mode = await cached_mode(user_id)
    normalized = normalize_answer(message.text)

    if user_id in pending_cancel:
        pending_cancel.pop(user_id, None)

    if user_id in recent_reminders:
        task_info = recent_reminders[user_id]
        task_id = task_info["task_id"]
        task_text = task_info["task_text"]

        if normalized in DONE_WORDS:
            await mark_task_status(task_id, "done")
            del recent_reminders[user_id]
            stats = await get_user_stats(user_id)
            await message.answer(reaction_done(user_id, mode, stats))
            return

        if normalized in FAIL_WORDS:
            await mark_task_status(task_id, "failed")
            del recent_reminders[user_id]
            stats = await get_user_stats(user_id)
            await message.answer(reaction_fail(user_id, mode, stats))
            return

        del recent_reminders[user_id]
        context = f"Только что было напоминание по задаче: «{task_text}». Пользователь отвечает нестандартно."
        if not await cached_premium(user_id):
            limits = await get_limits(user_id)
            current = await increment_chat_requests(user_id)
            if current > limits["chat_limit"]:
                await message.answer(
                    f"🛑 *Лимит запросов исчерпан.*\n\n"
                    f"Бесплатно: {limits['chat_limit']} в день.\n"
                    f"Пригласи друга (/invite) или оформи Premium ⤵",
                    reply_markup=premium_menu_keyboard(),
                    parse_mode="Markdown",
                )
                return
        answer = ask_groq(user_id, message.text, mode, extra_context=context)
        await message.answer(answer)
        return

    if not await cached_premium(user_id):
        limits = await get_limits(user_id)
        current = await increment_chat_requests(user_id)
        if current > limits["chat_limit"]:
            await message.answer(
                f"🛑 *Лимит запросов исчерпан.*\n\n"
                f"Бесплатно: {limits['chat_limit']} в день.\n"
                f"Пригласи друга (/invite) или оформи Premium ⤵",
                reply_markup=premium_menu_keyboard(),
                parse_mode="Markdown",
            )
            return

    answer = ask_groq(user_id, message.text, mode)
    await message.answer(answer)


@dp.message(F.text)
async def handle_message(message: Message):
    user_id = message.from_user.id

    if user_id in pending_referrals:
        referrer_id = pending_referrals.pop(user_id)
        success, msg = await register_referral(user_id, referrer_id)
        if success:
            try:
                await bot.send_message(
                    chat_id=referrer_id,
                    text=f"🎉 *Твой друг зашёл в бота!*\n\n{msg}!\n"
                         f"Смотри свои бонусы в /invite 🤝",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logging.warning("Не смог уведомить реферера: %s", e)

    await update_last_active(user_id)
    await mark_user_started(user_id)

    if user_id in awaiting_promo:
        text = message.text.strip()

        if text.lower() in ("отмена", "выход", "cancel", "назад", "стоп", "/cancel"):
            awaiting_promo.discard(user_id)
            await message.answer("Ок, выходим из режима промокода 🤝", reply_markup=chat_keyboard())
            return

        menu_buttons = {"⚙️ Настройки", "⬅️ В чат", "💎 Premium"} | set(MODE_BUTTONS.keys())
        if text in menu_buttons:
            awaiting_promo.discard(user_id)
            if text == "⚙️ Настройки":
                await cmd_settings(message)
            elif text == "⬅️ В чат":
                await message.answer("Ок, вернулся в обычный режим 🤝", reply_markup=chat_keyboard())
            elif text == "💎 Premium":
                await cmd_premium(message)
            elif text in MODE_BUTTONS:
                mode = MODE_BUTTONS[text]
                await set_user_mode(user_id, mode)
                cache_set(user_id, mode=mode)
                dialogue_memory[user_id].clear()
                await message.answer(MODE_REPLIES[mode], reply_markup=settings_keyboard())
            return

        code = text.upper()
        if not re.match(r"^[A-Z0-9\-]{4,20}$", code):
            await message.answer(
                "Хм, это не похоже на промокод.\n\n"
                "Попробуй ещё раз или напиши «отмена», чтобы выйти."
            )
            return

        awaiting_promo.discard(user_id)
        success, msg = await redeem_promocode(user_id, code)
        if success:
            cache_set(user_id, premium=True)
            await message.answer(f"✅ {msg}\n\nНаслаждайся безлимитом, бро! 🤝")
        else:
            await message.answer(f"❌ {msg}")
        return

    await route_to_chat(message)


# ---------- Восстановление задач ----------
async def restore_pending_tasks():
    for task in await get_pending_tasks():
        remind_at = datetime.fromisoformat(task["remind_at"])
        if remind_at <= datetime.now():
            remind_at = datetime.now() + timedelta(seconds=10)
        mode = await get_user_mode(task["user_id"])
        scheduler.add_job(
            send_reminder,
            trigger=DateTrigger(run_date=remind_at),
            args=[task["user_id"], task["id"], task["text"], mode],
            id=f"task_{task['id']}",
            replace_existing=True,
        )
        logging.info("Восстановлена задача #%s на %s", task["id"], remind_at)


# ---------- Health + Webhook ----------
async def health(request):
    return web.Response(text="Bot is alive")


async def on_startup(bot: Bot) -> None:
    webhook_url = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query", "pre_checkout_query"],
    )
    logging.info("Webhook установлен: %s", webhook_url)


async def on_shutdown(bot: Bot) -> None:
    await bot.delete_webhook()
    logging.warning("Webhook удалён. Завершение работы...")


async def main():
    await init_db()
    scheduler.start()
    await restore_pending_tasks()

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    app.router.add_get("/", health)

    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("Сервер слушает порт %s (webhook + health)", PORT)

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
