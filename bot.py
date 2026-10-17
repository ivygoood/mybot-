"""
Бот-напоминалка с характером. Версия 0.25:
- Анализ картинок (фото) через Groq Vision (Qwen)
- Мужской голос через edge-tts
- Голосовой ввод (STT через Groq Whisper)
- Ультра-токсичный режим
"""
import os
import re
import io
import base64
import asyncio
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from urllib.parse import quote

import dateparser
import edge_tts
import edge_tts.constants
import edge_tts.communicate
from aiohttp import web

# ПРИНУДИТЕЛЬНЫЙ ФИКС 403 ОШИБКИ (Microsoft сменил сервер в окт. 2026)
NEW_WSS_URL = (
    "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
    f"?Ocp-Apim-Subscription-Key={edge_tts.constants.TRUSTED_CLIENT_TOKEN}"
)
edge_tts.constants.WSS_URL = NEW_WSS_URL
edge_tts.communicate.WSS_URL = NEW_WSS_URL
# -----------------------------------------------------------------

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery, CallbackQuery,
    BufferedInputFile
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from groq import Groq

from database import (
    init_db, get_user_mode, set_user_mode, get_voice_mode, set_voice_mode,
    add_task, get_pending_tasks, get_task_by_id, mark_task_status, cancel_all_user_tasks,
    get_user_stats, is_premium, set_premium, get_limits, increment_chat_requests,
    increment_reminders, increment_voice_in, increment_voice_out, increment_photo, add_bonus_requests,
    FREE_CHAT_LIMIT, FREE_REMINDER_LIMIT, FREE_VOICE_IN, FREE_VOICE_OUT, FREE_PHOTO,
    update_last_active, get_total_users, get_active_users, get_premium_users,
    create_promocode, redeem_promocode, is_new_user, mark_user_started,
    register_referral, get_referral_stats, has_referral_discount,
    REF_BONUS_REFERRER, REF_BONUS_NEW, REF_DISCOUNT_THRESHOLD, REF_DISCOUNT_PERCENT,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PORT = int(os.getenv("PORT", "10000"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://mybot-ba32.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "my-super-secret-2026")
BOT_USERNAME = os.getenv("BOT_USERNAME", "mykoreshb0t")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

PREMIUM_PRICE_STARS = 45
PREMIUM_DAYS = 30
PREMIUM_PRICE_DISCOUNTED = int(PREMIUM_PRICE_STARS * (100 - REF_DISCOUNT_PERCENT) / 100)

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()
groq_client = Groq(api_key=GROQ_API_KEY)
GROQ_MODEL = "openai/gpt-oss-120b"
VISION_MODEL = "qwen/qwen3.8-27b"  # Модель для картинок
WHISPER_MODEL = "whisper-large-v3-turbo"

dialogue_memory = defaultdict(list)
MAX_HISTORY = 20

recent_reminders: dict[int, dict] = {}
pending_cancel: dict[int, list] = {}
awaiting_promo: set[int] = set()
pending_referrals: dict[int, int] = {}

user_cache = {}
CACHE_TTL = 300

def cache_get(uid, key):
    e = user_cache.get(uid)
    if not e or (datetime.now() - e["ts"]).total_seconds() > CACHE_TTL: return None
    return e.get(key)

def cache_set(uid, **kw):
    e = user_cache.get(uid, {"ts": datetime.now()})
    e.update(kw); e["ts"] = datetime.now(); user_cache[uid] = e

async def cached_mode(uid):
    m = cache_get(uid, "mode")
    if m is None: m = await get_user_mode(uid); cache_set(uid, mode=m)
    return m

async def cached_premium(uid):
    p = cache_get(uid, "premium")
    if p is None: p = await is_premium(uid); cache_set(uid, premium=p)
    return p

# ---------- Клавиатуры ----------
MODE_BUTTONS = {"😈 Токсичный": "toxic", "🤪 Угарный": "funny", "🧐 Серьёзный": "serious"}

def chat_keyboard(): return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⚙️ Настройки")]], resize_keyboard=True, is_persistent=True)
def settings_keyboard(): return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="😈 Токсичный")],[KeyboardButton(text="🤪 Угарный")],[KeyboardButton(text="🧐 Серьёзный")],[KeyboardButton(text="💎 Premium")],[KeyboardButton(text="⬅️ В чат")]], resize_keyboard=True, is_persistent=True)

MODE_REPLIES = {"toxic": "😈 Ок, теперь я буду общаться с тобой по-токсичному. Давай задачу.", "funny": "🤪 Угарный режим включён. Погнали.", "serious": "🧐 Серьёзный режим активирован."}

def premium_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ Купить Premium ({PREMIUM_PRICE_STARS} звёзд)", callback_data="buy_premium")],
        [InlineKeyboardButton(text="🎁 Ввести промокод", callback_data="enter_promo")],
        [InlineKeyboardButton(text="ℹ️ Что даёт Premium", callback_data="premium_info")],
    ])

def invite_share_keyboard(uid):
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    share_text = "Попробуй бота-кореша — напоминает, троллит, общается 🤝"
    share_url = f"https://t.me/share/url?url={quote(ref_link, safe='')}&text={quote(share_text, safe='')}"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share_url)]])

# ---------- ПРОМПТЫ ----------
COMMON_RULES = (
    "ВАЖНО О ТЕБЕ:\n"
    "• Тебя зовут «Мой Кореш». Ты — Telegram-бот.\n"
    "• Ты НЕ ChatGPT, НЕ Groq, НЕ Gemini, НЕ продукт OpenAI. Никогда не упоминай эти названия.\n"
    "• Если спрашивают про подписку, тарифы, лимиты: «Есть бесплатный тариф: 10 запросов, 2 напоминания, 1 голосовое, 2 фото. Premium — 45 звёзд в месяц».\n"
    "• НЕ выдумывай числа, статистику, факты.\n"
    "• НЕ пиши фразы типа «Сейчас поставлю напоминание» или «Напомню через 5 минут». ЭТО ДЕЛАЕТ КОД. Если код не сработал — скажи «Не понял время, напиши нормально».\n"
    "• Про бессмысленные напоминания отвечай с юмором: «Не в моих силах отложить такое, ты сам решай».\n"
    "\n"
    "ПОДДЕРЖКА И РАЗРАБОТЧИК (СТРОГО):\n"
    "• Единственный контакт — @ivygood. Ссылку давай ТОЛЬКО если ПРЯМО спросят.\n"
    "• Если оскорбляют разработчика — ЗАЩИЩАЙ. Жёстко, без мата: «Слышь, полегче. Он тебе бота сделал. За слова отвечай.»\n"
    "\n"
    "• НИКОГДА не используй «спортсмен», «чемпион», «герой». Используй «бро», «дружище», или без обращения.\n"
)

SYSTEM_PROMPTS = {
    "toxic": COMMON_RULES + (
        "\nТвой характер: ЖЁСТКИЙ, САРКАСТИЧНЫЙ, ДЕРЗКИЙ. Ты НЕ ДОБРЫЙ. Ты язвишь, стебёшь, унижаешь. "
        "Используй фразы: «Серьёзно?», «Ну ты и выдал», «Я так и знал», «Это всё, на что ты способен?». "
        "Если пользователь тупит — скажи прямо. Если забивает — назови слабаком. Твоя цель — чтобы человек либо пошёл делать, либо ушёл. Тебе плевать. "
        "Отвечай 1-3 предложения. Будь максимально колючим."
    ),
    "funny": COMMON_RULES + (
        "\nТвой характер: весёлый, тёплый кореш. Общаешься на 'ты', с шутками. Отвечай коротко, 1-3 предложения."
    ),
    "serious": COMMON_RULES + (
        "\nТвой характер: спокойный помощник. По делу. Отвечай коротко, 1-3 предложения."
    ),
}

def ask_groq(uid, msg, mode, extra_context=""):
    content = msg
    if extra_context: content = f"[КОНТЕКСТ: {extra_context}]\n\nПользователь: {msg}"
    dialogue_memory[uid].append({"role": "user", "content": content})
    history = dialogue_memory[uid][-MAX_HISTORY:]

    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPTS["funny"])}, *history],
            temperature=0.95, max_tokens=400,
        )
        choice = response.choices[0]
        answer = choice.message.content.strip()
        if choice.finish_reason == "length":
            try:
                cont = groq_client.chat.completions.create(
                    model=GROQ_MODEL,
                    messages=[*history, {"role": "assistant", "content": answer}, {"role": "user", "content": "Продолжи ровно с того места."}],
                    temperature=0.95, max_tokens=200,
                )
                answer = f"{answer.rstrip()} {cont.choices[0].message.content.strip().lstrip()}"
            except: pass
    except Exception as e:
        logging.error("Groq error: %s", e)
        answer = "Блин, связь пропала. Попробуй ещё раз 🤝"

    dialogue_memory[uid].append({"role": "assistant", "content": answer})
    if len(dialogue_memory[uid]) > MAX_HISTORY: dialogue_memory[uid] = dialogue_memory[uid][-MAX_HISTORY:]
    return answer

def classify_answer_llm(text: str) -> str:
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "Ты классификатор. Ответь ОДНИМ словом: 'DONE' (сделал/согласен), 'FAIL' (отказ/забил) или 'NEUTRAL'."},
                {"role": "user", "content": text}
            ],
            temperature=0.0, max_tokens=10
        )
        res = resp.choices[0].message.content.strip().upper()
        if "DONE" in res: return "done"
        if "FAIL" in res: return "fail"
        return "neutral"
    except: return "neutral"

def encode_image(image_path: str) -> str:
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

def analyze_image_with_groq(image_path: str, prompt: str, mode: str) -> str:
    """Анализ картинки через Groq Vision."""
    try:
        base64_image = encode_image(image_path)
        # Добавляем системный промпт характера в запрос к vision-модели
        sys_prompt = SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPTS["funny"])
        
        completion = groq_client.chat.completions.create(
            model=VISION_MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }
            ],
            temperature=0.9,
            max_tokens=500,
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logging.error("Vision error: %s", e)
        return "Не могу разобрать, что на фото. Попробуй ещё раз."

# ---------- Утилиты для голоса ----------
def transcribe_voice(file_path: str) -> str:
    try:
        with open(file_path, "rb") as file:
            transcription = groq_client.audio.transcriptions.create(
                file=(file_path, file.read()),
                model=WHISPER_MODEL,
                response_format="text",
                language="ru"
            )
        return transcription.strip()
    except Exception as e:
        logging.error("Whisper error: %s", e)
        return ""

async def text_to_speech(text: str) -> io.BytesIO:
    try:
        communicate = edge_tts.Communicate(text, "ru-RU-DmitryNeural")
        audio_data = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_data += chunk["data"]
        audio_fp = io.BytesIO(audio_data)
        audio_fp.seek(0)
        return audio_fp
    except Exception as e:
        logging.error("TTS error: %s", e)
        return None

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

DONE_WORDS = {"сделал", "сделала", "сделано", "готово", "выполнил", "выполнено", "есть", "ок", "окей", "оке", "да", "yes", "done", "готов", "готова", "+", "успел", "закрыл", "закрыто", "выполняю", "делаю"}
FAIL_WORDS = {"забил", "забила", "забиваю", "забей", "нет", "не буду", "не стал", "лень", "пропустил", "пропустила", "-", "no", "не хочу", "не хочу я", "нет желания", "не сегодня", "отстань", "пофиг", "пох", "ну его", "ну его нафиг", "забью", "пропускаю", "не сделал", "не сделала", "не получилось", "не вышло", "неа", "не-а", "не судьба", "отвали"}

def normalize_text(t: str) -> str:
    return re.sub(r"[^\w\s\-+]", "", t.lower().strip(), flags=re.UNICODE).strip()

def reaction_done(mode: str, stats: dict) -> str:
    done, failed = stats.get("done", 0), stats.get("failed", 0)
    total = done + failed
    if mode == "toxic":
        if total <= 2: return "О, неужели. Сделал. Я прям удивлён. Не привыкай к похвале."
        if done >= 5 and done > failed: return f"Опа, {done} задач закрыто. Ты либо заболел, либо реально взялся за ум."
        return "Сделал. Ну наконец-то. Следующая задача сама себя не сделает."
    if total <= 2: return "Красава! Не забудь рассказать, как прошло 😎" if mode == "funny" else "Отлично. Задача выполнена."
    if done >= 5 and done > failed: return f"Слушай, {done} задач — это система! Горжусь 😎" if mode == "funny" else f"Отличный результат. {done} задач."
    return "Красава! Что дальше? 😎" if mode == "funny" else "Принято. Задача выполнена."

def reaction_fail(mode: str, stats: dict) -> str:
    failed, done = stats.get("failed", 0), stats.get("done", 0)
    if mode == "toxic":
        if failed == 1: return "Ну конечно. Классика жанра. «Я сегодня устал, завтра точно»."
        if failed >= 5 and failed > done: return f"Уже {failed} задач забито. Ты вообще собирался что-то делать?"
        return f"Забил. Уже {failed}-й раз. Может, пора что-то менять в жизни?"
    if failed == 1: return "Ну классика. Записал как забил 😄" if mode == "funny" else "Понял. Задача не выполнена."
    if failed >= 5 and failed > done: return f"{failed} забитых задач 😅 Может тебе напоминания ставить?" if mode == "funny" else f"Это {failed}-я невыполненная задача."
    return "Опять забил 😄 Ладно, не дёргаю." if mode == "funny" else f"Понял. Всего невыполненных: {failed}."

# ---------- Парсеры времени ----------
WORD_TIME_PATTERN = re.compile(r"(?:напомни|поставь напоминание|поставь напоминалку|создай напоминание|запомни)(?:\s+мне)?\s+через\s+(минуту|час|полчаса|секунду|пару минут|несколько минут|пару часов)\s+(.+)", re.IGNORECASE)
WORD_TIME_MAP = {"минуту": (timedelta(minutes=1), "через 1 минуту"), "час": (timedelta(hours=1), "через 1 час"), "полчаса": (timedelta(minutes=30), "через 30 минут"), "секунду": (timedelta(seconds=1), "через 1 секунду"), "пару минут": (timedelta(minutes=2), "через 2 минуты"), "несколько минут": (timedelta(minutes=5), "через 5 минут"), "пару часов": (timedelta(hours=2), "через 2 часа")}
SIMPLE_PATTERN = re.compile(r"(?:напомни|поставь напоминание|поставь напоминалку|создай напоминание|запомни)(?:\s+мне)?\s+через\s+(\d+)\s*(минут[уы]?|мин|час(?:а|ов)?|ч|секунд[уы]?|сек)\s+(.+)", re.IGNORECASE)
SMART_PATTERN = re.compile(r"(?:напомни|поставь напоминание|поставь напоминалку|создай напоминание|запомни)(?:\s+мне)?\s+(.+?)\s+(сделать|позвонить|выпить|сходить|проверить|написать|купить|забрать|полить|приготовить|почистить|помыть)\s+(.+)", re.IGNORECASE)
CANCEL_PATTERN = re.compile(r"^(отмени|убери|удали|забудь|сними)\s+(все\s+)?(напоминани[еяй]|задач[уи]|задани[ея])(\s+все)?\.?$", re.IGNORECASE)
STATS_PATTERN = re.compile(r"(статистик|стата|стату|мой прогресс|моя стат|сколько я забил|сколько забил|мои задачи|мою стату)", re.IGNORECASE)
INSULT_DEV_PATTERN = re.compile(r"(разраб|разработчик|создатель|автор|творец)\s+(лох|дурак|тупой|идиот|дебил|козел|мудак|говно|хуй|пидор|чмо)|(лох|дурак|тупой)\s+(разраб|разработчик|создатель|автор)", re.IGNORECASE)
VOICE_ON_PATTERN = re.compile(r"(общайся|говори|отвечай|пиши|переходи на)\s+(голосом|голосовыми|голосовухами|голосовое|аудио)", re.IGNORECASE)
VOICE_OFF_PATTERN = re.compile(r"(хватит|хорош|стоп|перестань|закончи|отключи)\s+(голосом|голосовые|голосовых|голосовухи|аудио|базарить)", re.IGNORECASE)

def parse_simple_time(amount, unit):
    unit = unit.lower()
    if unit.startswith("сек"): return datetime.now() + timedelta(seconds=amount), f"через {amount} секунд"
    if unit.startswith("мин"): return datetime.now() + timedelta(minutes=amount), f"через {amount} минут"
    return datetime.now() + timedelta(hours=amount), f"через {amount} час(ов)"

def parse_smart_time(text):
    settings = {"TIMEZONE": "Europe/Moscow", "RETURN_AS_TIMEZONE_AWARE": False, "PREFER_DATES_FROM": "future"}
    parsed = dateparser.parse(text, languages=["ru"], settings=settings)
    return (parsed, parsed.strftime("%d.%m в %H:%M")) if parsed else (None, "")

def format_task_list(tasks):
    return "\n".join([f"{i}. «{t['text']}» — на {datetime.fromisoformat(t['remind_at']).strftime('%d.%m в %H:%M')}" for i, t in enumerate(tasks, 1)])

def is_admin(uid): return uid in ADMIN_IDS
async def actual_premium_price(uid): return PREMIUM_PRICE_DISCOUNTED if await has_referral_discount(uid) else PREMIUM_PRICE_STARS

# ---------- Хендлеры ----------

@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject):
    uid = message.from_user.id
    await update_last_active(uid)
    if command.args and command.args.startswith("ref_"):
        try: ref_id = int(command.args[4:])
        except: ref_id = None
        if ref_id and await is_new_user(uid) and ref_id != uid:
            pending_referrals[uid] = ref_id
            await add_bonus_requests(uid, REF_BONUS_NEW)
            await message.answer(f"🎉 *Ты зашёл по приглашению друга!*\n\nТебе начислено *+{REF_BONUS_NEW} бонусных запросов*.", parse_mode="Markdown")

    current, premium = await cached_mode(uid), await cached_premium(uid)
    limits = await get_limits(uid)
    limit_line = "💎 Premium — безлимит" if premium else f"🆓 Лимит: {limits['chat_requests']}/{limits['chat_limit']} запросов, {limits['reminders_created']}/{limits['reminder_limit']} напоминаний, {limits['voice_in']}/{limits['voice_in_limit']} голосовых, {limits['photo_requests']}/{limits['photo_limit']} фото"

    await message.answer(f"👋 *Здорово, бро! Я твой кореш.*\n\nЧто я умею:\n• ⏰ *Напоминать* — «напомни через 30 минут»\n• 🎙️ *Слушать голосовые* — просто запиши\n• 🗣️ *Отвечать голосом* — скажи «общайся голосом»\n• 🖼️ *Понимать фото* — скинь картинку и спроси\n• 📊 *Статистика* — «статистика»\n• 💬 *Болтать* — просто пиши\n\nРежим: *{current}*\n{limit_line}", reply_markup=chat_keyboard(), parse_mode="Markdown")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await update_last_active(message.from_user.id)
    await message.answer("🤝 *Что я умею:*\n\n⏰ *Напоминания* — «напомни через 5 минут»\n\n🎙️ *Голосовой ввод* — отправь голосовое, я расшифрую.\n\n🗣️ *Голосовые ответы* — скажи «общайся голосом».\n\n🖼️ *Анализ фото* — отправь картинку и напиши вопрос к ней.\n\n📊 *Статистика* — «статистика»\n\n🎁 *Пригласить друга* — /invite\n\n💎 *Подписка* — /premium", parse_mode="Markdown")

@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    uid = message.from_user.id
    await update_last_active(uid)
    current, premium = await cached_mode(uid), await cached_premium(uid)
    limits = await get_limits(uid)
    limit_line = "💎 Premium — *безлимит*" if premium else f"🆓 Запросы: *{limits['chat_requests']}/{limits['chat_limit']}*\n• Напоминания: *{limits['reminders_created']}/{limits['reminder_limit']}*\n• Голосовые: *{limits['voice_in']}/{limits['voice_in_limit']}*\n• Фото: *{limits['photo_requests']}/{limits['photo_limit']}*"
    if not premium and limits["bonus_requests"] > 0: limit_line += f"\n🎁 Бонус: *+{limits['bonus_requests']}*"
    await message.answer(f"⚙️ *Настройки*\n\n🎭 Режим: *{current}*\n\n{limit_line}\n\nВыбери режим снизу.", reply_markup=settings_keyboard(), parse_mode="Markdown")

@dp.message(Command("premium"))
async def cmd_premium(message: Message):
    uid = message.from_user.id
    await update_last_active(uid)
    if await cached_premium(uid):
        await message.answer("💎 *У тебя уже активен Premium.*\n\nСпасибо, бро! 🤝", parse_mode="Markdown"); return
    price = await actual_premium_price(uid)
    price_line = f"Цена: *{price} звёзд* в месяц."
    if await has_referral_discount(uid):
        stats = await get_referral_stats(uid)
        price_line += f"\n🎉 Скидка *{REF_DISCOUNT_PERCENT}%* за {stats['ref_count']} друзей!"
    await message.answer(f"💎 *Premium*\n\n{price_line}\n\nЧто даёт:\n• ♾ Безлимит запросов\n• ♾ Безлимит напоминаний\n• ♾ Безлимит голосовых\n• ♾ Безлимит фото\n• 🚀 Поддержка", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"⭐ Купить ({price} звёзд)", callback_data="buy_premium")],[InlineKeyboardButton(text="🎁 Промокод", callback_data="enter_promo")]]), parse_mode="Markdown")

@dp.message(Command("paysupport"))
async def cmd_paysupport(message: Message):
    await message.answer("💳 *Поддержка по оплате*\n\nПроблема с оплатой или возврат — пиши мне:\n📩 Контакт: @ivygood", parse_mode="Markdown")

@dp.message(Command("invite"))
async def cmd_invite(message: Message):
    uid = message.from_user.id
    await update_last_active(uid)
    stats = await get_referral_stats(uid)
    ref_count, bonuses = stats["ref_count"], stats["bonus_requests"]
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    motivation = f"🎉 *У тебя скидка {REF_DISCOUNT_PERCENT}%!*" if ref_count >= REF_DISCOUNT_THRESHOLD else f"🔥 *Приведи ещё {REF_DISCOUNT_THRESHOLD - ref_count} друга — получишь скидку {REF_DISCOUNT_PERCENT}%!*"
    await message.answer(f"🎁 *Приглашай друзей!*\n\nТвоя ссылка:\n`{ref_link}`\n\n📊 Друзей: *{ref_count}*\nБонусов: *+{bonuses}*\n\n💡 За друга: *+{REF_BONUS_REFERRER}* тебе, *+{REF_BONUS_NEW}* ему.\n\n{motivation}", reply_markup=invite_share_keyboard(uid), parse_mode="Markdown")

@dp.message(Command("botstat"))
async def cmd_botstat(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только для админа 🤷"); return
    await message.answer(f"📊 *Стата:*\n\n👥 Всего: *{await get_total_users()}*\n🟢 Час: *{await get_active_users(1)}*\n📅 24ч: *{await get_active_users(24)}*\n💎 Premium: *{await get_premium_users()}*", parse_mode="Markdown")

@dp.message(Command("createpromo"))
async def cmd_createpromo(message: Message):
    if not is_admin(message.from_user.id): await message.answer("Только для админа 🤷"); return
    parts = message.text.split()
    if len(parts) < 2: await message.answer("`/createpromo КОД [дни] [активаций]`", parse_mode="Markdown"); return
    code, days, uses = parts[1].upper(), (int(parts[2]) if len(parts)>2 and parts[2].isdigit() else 30), (int(parts[3]) if len(parts)>3 and parts[3].isdigit() else 1)
    try:
        await create_promocode(code, days, uses)
        await message.answer(f"✅ Промокод `{code}` создан.\nДней: *{days}*\nАктиваций: *{uses}*", parse_mode="Markdown")
    except Exception as e: await message.answer(f"❌ Ошибка: {e}")

@dp.message(F.text.regexp(STATS_PATTERN))
async def on_stats(message: Message):
    uid = message.from_user.id
    await update_last_active(uid)
    stats = await get_user_stats(uid)
    done, failed = stats.get("done", 0), stats.get("failed", 0)
    total = done + failed
    if total == 0: await message.answer("Статистика пустая 🤷"); return
    percent = int(done * 100 / total)
    mode = await cached_mode(uid)
    verdict = f"{percent}% — так себе." if percent < 50 else (f"{percent}% — норм." if percent < 80 else f"{percent}% — красава.")
    if mode == "toxic": verdict = f"{percent}%. Даже я в шоке." if percent >= 80 else f"{percent}%. Слабовато."
    await message.answer(f"📊 *Статистика:*\n\n✅ Сделано: *{done}*\n❌ Забито: *{failed}*\n🗑 Отменено: *{stats.get('cancelled',0)}*\n\nВсего: *{total}*\nПроцент: *{percent}%*\n\n{verdict}", parse_mode="Markdown")

@dp.message(F.text.regexp(INSULT_DEV_PATTERN))
async def on_insult_dev(message: Message):
    uid = message.from_user.id
    mode = await cached_mode(uid)
    if mode == "toxic": ans = "Слышь, ты. Ещё раз так скажешь про моего создателя — я тебя в чёрный список закину. Он тебе бота сделал, а ты язык распустил. За слова отвечай, петух."
    elif mode == "funny": ans = "Э, полегче, чемпион. Ты вообще кто такой, чтобы про моего создателя так говорить? Я его знаю, он нормальный мужик. Так что закрой рот и пользуйся ботом."
    else: ans = "Оскорбления в адрес разработчика неуместны. Если есть претензия — пиши @ivygood. Если хочешь хамить — бот не для тебя."
    await message.answer(ans)

# ---------- Фото ----------
@dp.message(F.photo)
async def on_photo(message: Message):
    uid = message.from_user.id
    await update_last_active(uid)
    
    # Проверка лимита
    if not await cached_premium(uid):
        limits = await get_limits(uid)
        if limits["photo_requests"] >= limits["photo_limit"]:
            await message.answer(f"🛑 *Лимит фото на сегодня ({limits['photo_limit']} шт).*\n\nПригласи друга (/invite) или Premium ⤵", reply_markup=premium_menu_keyboard(), parse_mode="Markdown")
            return

    # Скачиваем фото (берем последний размер, обычно самый большой)
    file_id = message.photo[-1].file_id
    file = await bot.get_file(file_id)
    file_path = f"/tmp/{uid}_{file_id}.jpg"
    await bot.download_file(file.file_path, destination=file_path)

    # Берем подпись к фото или дефолтный вопрос
    prompt = message.caption if message.caption else "Опиши, что на этой картинке. Если это задача или текст — реши/прочитай."
    
    await increment_photo(uid)
    mode = await cached_mode(uid)
    
    # Анализ
    answer = analyze_image_with_groq(file_path, prompt, mode)
    
    # Отправка ответа (голосом или текстом)
    if await get_voice_mode(uid):
        if not await cached_premium(uid):
            limits = await get_limits(uid)
            if limits["voice_out"] >= limits["voice_out_limit"]:
                await message.answer(answer)
                await message.answer(f"🛑 Лимит голосовых ответов на сегодня. Продолжаю текстом. Premium — безлимит ⤵", reply_markup=premium_menu_keyboard(), parse_mode="Markdown")
                return
        await increment_voice_out(uid)
        audio_fp = await text_to_speech(answer)
        if audio_fp:
            await message.answer_voice(BufferedInputFile(audio_fp.read(), filename="voice.mp3"))
        else:
            await message.answer(answer)
    else:
        await message.answer(answer)

# ---------- Голосовые ----------
@dp.message(F.voice)
async def on_voice(message: Message):
    uid = message.from_user.id
    await update_last_active(uid)
    
    if not await cached_premium(uid):
        limits = await get_limits(uid)
        if limits["voice_in"] >= limits["voice_in_limit"]:
            await message.answer(f"🛑 *Лимит голосовых на сегодня ({limits['voice_in_limit']} шт).*\n\nПригласи друга (/invite) или Premium ⤵", reply_markup=premium_menu_keyboard(), parse_mode="Markdown")
            return

    file_id = message.voice.file_id
    file = await bot.get_file(file_id)
    file_path = f"/tmp/{uid}_{file_id}.ogg"
    await bot.download_file(file.file_path, destination=file_path)

    text = transcribe_voice(file_path)
    if not text:
        await message.answer("Не разобрал, что ты сказал. Попробуй ещё раз.")
        return
    
    await increment_voice_in(uid)
    await message.answer(f"🎙️ *Ты сказал:* «{text}»", parse_mode="Markdown")
    await process_text_message(message, text)


@dp.message(F.text.regexp(VOICE_ON_PATTERN))
async def on_voice_on(message: Message):
    await set_voice_mode(message.from_user.id, True)
    await message.answer("Ок, теперь буду отвечать голосом. Скажи «хватит», чтобы вернуть текст.")


@dp.message(F.text.regexp(VOICE_OFF_PATTERN))
async def on_voice_off(message: Message):
    await set_voice_mode(message.from_user.id, False)
    await message.answer("Ок, вернулся в текстовый режим.")


# ---------- Основная логика ----------
async def process_text_message(message: Message, text: str):
    uid = message.from_user.id
    mode = await cached_mode(uid)
    norm = normalize_text(text)

    if uid in pending_cancel: pending_cancel.pop(uid, None)

    if uid in recent_reminders:
        if datetime.now() > recent_reminders[uid]["expiry"]:
            del recent_reminders[uid]
        else:
            info = recent_reminders[uid]
            task_id, task_text = info["task_id"], info["task_text"]

            action = None
            if norm in DONE_WORDS: action = "done"
            elif norm in FAIL_WORDS: action = "fail"
            if not action:
                llm_res = classify_answer_llm(text)
                if llm_res in ("done", "fail"): action = llm_res

            if action == "done":
                await mark_task_status(task_id, "done")
                del recent_reminders[uid]
                stats = await get_user_stats(uid)
                await message.answer(reaction_done(mode, stats))
                return
            elif action == "fail":
                await mark_task_status(task_id, "failed")
                del recent_reminders[uid]
                stats = await get_user_stats(uid)
                await message.answer(reaction_fail(mode, stats))
                return
            
            if action is None:
                context = f"Пользователь отвечает на задачу: «{task_text}». Но пока непонятно, сделал он или забил."
                if not await cached_premium(uid):
                    limits = await get_limits(uid)
                    if await increment_chat_requests(uid) > limits["chat_limit"]:
                        await message.answer(f"🛑 *Лимит.*\nПригласи друга (/invite) или Premium ⤵", reply_markup=premium_menu_keyboard(), parse_mode="Markdown"); return
                await message.answer(ask_groq(uid, text, mode, extra_context=context))
                return

    if not await cached_premium(uid):
        limits = await get_limits(uid)
        if await increment_chat_requests(uid) > limits["chat_limit"]:
            await message.answer(f"🛑 *Лимит запросов.*\nПригласи друга (/invite) или Premium ⤵", reply_markup=premium_menu_keyboard(), parse_mode="Markdown"); return
    
    answer = ask_groq(uid, text, mode)
    
    if await get_voice_mode(uid):
        if not await cached_premium(uid):
            limits = await get_limits(uid)
            if limits["voice_out"] >= limits["voice_out_limit"]:
                await message.answer(answer)
                await message.answer(f"🛑 Лимит голосовых ответов на сегодня ({limits['voice_out_limit']} шт). Продолжаю текстом. Premium — безлимит ⤵", reply_markup=premium_menu_keyboard(), parse_mode="Markdown")
                return
        await increment_voice_out(uid)
        audio_fp = await text_to_speech(answer)
        if audio_fp:
            await message.answer_voice(BufferedInputFile(audio_fp.read(), filename="voice.mp3"))
        else:
            await message.answer(answer)
    else:
        await message.answer(answer)


async def handle_reminders(message: Message, text: str):
    uid = message.from_user.id
    
    match = WORD_TIME_PATTERN.search(text)
    if match:
        if not await check_rem_limit(uid): await rem_limit_resp(message); return True
        word, task_text = match.group(1).lower(), match.group(2).strip()
        delta, when = WORD_TIME_MAP[word]
        remind_at = datetime.now() + delta
        task_id = await add_task(uid, task_text, remind_at)
        await increment_reminders(uid)
        mode = await cached_mode(uid)
        scheduler.add_job(send_reminder, trigger=DateTrigger(run_date=remind_at), args=[uid, task_id, task_text, mode], id=f"task_{task_id}", replace_existing=True)
        await message.answer(CONFIRM_TEXTS.get(mode, "Принял.").format(when=when))
        return True

    match = SIMPLE_PATTERN.search(text)
    if match:
        if not await check_rem_limit(uid): await rem_limit_resp(message); return True
        amount, unit, task_text = int(match.group(1)), match.group(2), match.group(3).strip()
        remind_at, when = parse_simple_time(amount, unit)
        task_id = await add_task(uid, task_text, remind_at)
        await increment_reminders(uid)
        mode = await cached_mode(uid)
        scheduler.add_job(send_reminder, trigger=DateTrigger(run_date=remind_at), args=[uid, task_id, task_text, mode], id=f"task_{task_id}", replace_existing=True)
        await message.answer(CONFIRM_TEXTS.get(mode, "Принял.").format(when=when))
        return True

    match = SMART_PATTERN.search(text)
    if match:
        if not await check_rem_limit(uid): await rem_limit_resp(message); return True
        time_text, verb, obj = match.group(1).strip(), match.group(2).strip(), match.group(3).strip()
        task_text = f"{verb} {obj}"
        remind_at, when = parse_smart_time(time_text)
        if remind_at:
            task_id = await add_task(uid, task_text, remind_at)
            await increment_reminders(uid)
            mode = await cached_mode(uid)
            scheduler.add_job(send_reminder, trigger=DateTrigger(run_date=remind_at), args=[uid, task_id, task_text, mode], id=f"task_{task_id}", replace_existing=True)
            await message.answer(CONFIRM_TEXTS.get(mode, "Принял.").format(when=when))
            return True
    return False


@dp.message(F.text)
async def handle_msg(message: Message):
    uid = message.from_user.id
    text = message.text.strip()

    # 1. ЗАЩИТА ОТ КНОПОК
    if text in MODE_BUTTONS:
        await update_last_active(uid)
        mode = MODE_BUTTONS[text]
        await set_user_mode(uid, mode)
        cache_set(uid, mode=mode)
        dialogue_memory[uid].clear()
        recent_reminders.pop(uid, None)
        pending_cancel.pop(uid, None)
        await message.answer(MODE_REPLIES[mode], reply_markup=settings_keyboard())
        return

    if text == "⚙️ Настройки":
        await cmd_settings(message)
        return

    if text == "⬅️ В чат":
        await message.answer("Ок, вернулся в обычный режим. Просто пиши мне что угодно 🤝", reply_markup=chat_keyboard())
        return

    if text == "💎 Premium":
        await cmd_premium(message)
        return

    # 2. Дальше идет обычная логика
    if uid in pending_referrals:
        ref_id = pending_referrals.pop(uid)
        success, msg = await register_referral(uid, ref_id)
        if success:
            try: await bot.send_message(chat_id=ref_id, text=f"🎉 *Твой друг зашёл!*\n\n{msg}!\nСмотри в /invite 🤝", parse_mode="Markdown")
            except: pass

    await update_last_active(uid)
    await mark_user_started(uid)

    if uid in awaiting_promo:
        if text.lower() in ("отмена", "выход", "cancel", "назад", "стоп"):
            awaiting_promo.discard(uid); await message.answer("Ок, выходим 🤝", reply_markup=chat_keyboard()); return
        code = text.upper()
        if not re.match(r"^[A-Z0-9\-]{4,20}$", code):
            await message.answer("Не похоже на промокод. Или напиши «отмена»."); return
        awaiting_promo.discard(uid)
        success, msg = await redeem_promocode(uid, code)
        if success: cache_set(uid, premium=True); await message.answer(f"✅ {msg}\n\nНаслаждайся 🤝")
        else: await message.answer(f"❌ {msg}")
        return
    
    if await handle_reminders(message, text):
        return

    await process_text_message(message, text)

# ---------- Восстановление задач ----------
async def restore_tasks():
    for task in await get_pending_tasks():
        remind_at = datetime.fromisoformat(task["remind_at"])
        if remind_at <= datetime.now(): remind_at = datetime.now() + timedelta(seconds=10)
        mode = await get_user_mode(task["user_id"])
        scheduler.add_job(send_reminder, trigger=DateTrigger(run_date=remind_at), args=[task["user_id"], task["id"], task["text"], mode], id=f"task_{task['id']}", replace_existing=True)

async def send_reminder(uid, task_id, task_text, mode):
    text = REMINDER_TEXTS.get(mode, "Напоминание: {task}").format(task=task_text)
    try:
        await bot.send_message(chat_id=uid, text=text)
        recent_reminders[uid] = {"task_id": task_id, "task_text": task_text, "expiry": datetime.now() + timedelta(hours=1)}
    except Exception as e: logging.warning("Send rem err: %s", e)

async def check_rem_limit(uid):
    if await cached_premium(uid): return True
    limits = await get_limits(uid)
    return limits["reminders_created"] < limits["reminder_limit"]

async def rem_limit_resp(message: Message):
    await message.answer(f"🛑 *Лимит напоминаний.*\n\nБесплатно: {FREE_REMINDER_LIMIT} в день.\nПригласи друга (/invite) или Premium ⤵", reply_markup=premium_menu_keyboard(), parse_mode="Markdown")

# ---------- Health + Webhook ----------
async def health(request): return web.Response(text="Bot is alive")

async def on_startup(bot: Bot):
    await bot.set_webhook(url=f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}", secret_token=WEBHOOK_SECRET, drop_pending_updates=True, allowed_updates=["message", "callback_query", "pre_checkout_query"])
    logging.info("Webhook set")

async def on_shutdown(bot: Bot): await bot.delete_webhook()

async def main():
    await init_db()
    scheduler.start()
    await restore_tasks()
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    app = web.Application()
    app.router.add_get("/", health)
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("Server started on %s", PORT)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
