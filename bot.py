import os
import json
import hmac
import hashlib
import random
import string
import urllib.parse
import asyncio
import threading
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

PRIZES_FILE = "prizes.json"
USED_CODES_FILE = "used_codes.json"
HISTORY_FILE = "history.json"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

app = FastAPI(title="iGadget Wheel API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def read_json_file(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json_file(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def ensure_files():
    if not os.path.exists(PRIZES_FILE):
        write_json_file(PRIZES_FILE, [
            {
                "id": 1,
                "title": "Скидка 5% на аксессуары",
                "short": "-5%",
                "description": "Скидка 5% на аксессуары.",
                "weight": 35,
                "active": True
            },
            {
                "id": 2,
                "title": "Скидка 10% на аксессуары",
                "short": "-10%",
                "description": "Скидка 10% на аксессуары.",
                "weight": 22,
                "active": True
            },
            {
                "id": 3,
                "title": "Скидка 15% на аксессуары",
                "short": "-15%",
                "description": "Скидка 15% на аксессуары.",
                "weight": 12,
                "active": True
            },
            {
                "id": 4,
                "title": "Бесплатная доставка",
                "short": "Дост.",
                "description": "Бесплатная доставка на заказ.",
                "weight": 14,
                "active": True
            },
            {
                "id": 5,
                "title": "Подарок к покупке",
                "short": "Подарок",
                "description": "Небольшой подарок при следующей покупке.",
                "weight": 10,
                "active": True
            },
            {
                "id": 6,
                "title": "Бонус 500",
                "short": "500",
                "description": "500 бонусов на следующий заказ.",
                "weight": 7,
                "active": True
            }
        ])

    if not os.path.exists(USED_CODES_FILE):
        write_json_file(USED_CODES_FILE, [])

    if not os.path.exists(HISTORY_FILE):
        write_json_file(HISTORY_FILE, [])


ensure_files()


def parse_init_data(init_data: str) -> dict:
    parsed = urllib.parse.parse_qs(init_data, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def validate_init_data(init_data: str):
    if not init_data:
        return None

    data = parse_init_data(init_data)
    received_hash = data.pop("hash", None)
    if not received_hash or not BOT_TOKEN:
        return None

    data_check_arr = [f"{k}={v}" for k, v in sorted(data.items())]
    data_check_string = "\n".join(data_check_arr)

    secret_key = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode(),
        hashlib.sha256
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        return None

    user_raw = data.get("user")
    if not user_raw:
        return None

    try:
        return json.loads(user_raw)
    except Exception:
        return None


def get_user_from_init_data(init_data: str):
    return validate_init_data(init_data)


def load_prizes():
    return read_json_file(PRIZES_FILE, [])


def save_prizes(items):
    write_json_file(PRIZES_FILE, items)


def load_used_codes():
    return read_json_file(USED_CODES_FILE, [])


def save_used_codes(items):
    write_json_file(USED_CODES_FILE, items)


def load_history():
    return read_json_file(HISTORY_FILE, [])


def save_history(items):
    write_json_file(HISTORY_FILE, items)


def load_user_history(user_id):
    items = load_history()
    user_items = [x for x in items if str(x.get("user_id")) == str(user_id)]
    user_items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return user_items


def generate_code(length=6):
    chars = string.ascii_uppercase + string.digits
    return "IG-" + "".join(random.choice(chars) for _ in range(length))


def generate_unique_code():
    used_codes = load_used_codes()
    used_set = {str(x) for x in used_codes}

    for _ in range(50):
        code = generate_code()
        if code not in used_set:
            used_codes.append(code)
            save_used_codes(used_codes)
            return code

    raise Exception("Не удалось создать уникальный код")


def choose_weighted_prize(prizes):
    active_prizes = [p for p in prizes if p.get("active", True)]
    if not active_prizes:
        return None

    weighted = []
    for prize in active_prizes:
        weight = int(prize.get("weight", 1) or 1)
        weighted.extend([prize] * max(1, weight))

    return random.choice(weighted)


def today_key_msk():
    utc_now = datetime.now(timezone.utc)
    msk_now = utc_now + timedelta(hours=3)
    return msk_now.strftime("%Y-%m-%d")


def find_user_spin_today(user_id):
    items = load_user_history(user_id)
    key = today_key_msk()
    for item in items:
        if item.get("spin_date_msk") == key:
            return item
    return None


def is_admin(user):
    if not user:
        return False
    try:
        return int(user["id"]) in ADMIN_IDS
    except Exception:
        return False


@dp.message(CommandStart())
async def start_handler(message: Message):
    if not WEBAPP_URL:
        await message.answer("Не задан WEBAPP_URL в переменных окружения.")
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Бонусы iGadget",
                    web_app=WebAppInfo(url=WEBAPP_URL)
                )
            ]
        ]
    )

    await message.answer(
        "Добро пожаловать в iGadget Wheel.\nНажмите кнопку ниже, чтобы открыть колесо бонусов.",
        reply_markup=kb
    )


@dp.message(F.text == "Бонусы iGadget")
async def text_bonus_handler(message: Message):
    if not WEBAPP_URL:
        await message.answer("Не задан WEBAPP_URL в переменных окружения.")
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть колесо",
                    web_app=WebAppInfo(url=WEBAPP_URL)
                )
            ]
        ]
    )

    await message.answer("Откройте Mini App по кнопке ниже.", reply_markup=kb)


@app.get("/")
def root():
    return {"ok": True, "message": "Wheel API is running"}


@app.get("/prizes")
def prizes():
    items = load_prizes()
    active = [x for x in items if x.get("active", True)]
    return {"ok": True, "items": active}


@app.post("/spin")
async def spin(request: Request):
    data = await request.json()
    init_data = data.get("init_data", "")

    user = get_user_from_init_data(init_data)
    if not user:
        return JSONResponse(
            {"ok": False, "error": "Не удалось авторизовать пользователя"},
            status_code=401
        )

    user_id = str(user["id"])
    existing_today = find_user_spin_today(user_id)

    if existing_today:
        return {
            "ok": False,
            "cooldown": True,
            "error": "Сегодня вы уже использовали попытку.",
            "last_code": existing_today.get("code"),
            "last_prize_title": existing_today.get("prize_title"),
            "last_expires_at_text": existing_today.get("expires_at_text"),
        }

    prizes_data = load_prizes()
    prize = choose_weighted_prize(prizes_data)
    if not prize:
        return JSONResponse(
            {"ok": False, "error": "Нет активных призов"},
            status_code=400
        )

    code = generate_unique_code()
    created_at = datetime.utcnow()
    expires_at = created_at + timedelta(days=7)

    item = {
        "id": int(created_at.timestamp() * 1000),
        "user_id": user_id,
        "telegram_id": user_id,
        "first_name": user.get("first_name"),
        "username": user.get("username"),
        "prize_id": prize.get("id"),
        "prize_title": prize.get("title"),
        "prize_description": prize.get("description", ""),
        "code": code,
        "redeemed": False,
        "created_at": created_at.isoformat(),
        "created_at_text": created_at.strftime("%d.%m.%Y %H:%M"),
        "expires_at": expires_at.isoformat(),
        "expires_at_text": expires_at.strftime("%d.%m.%Y"),
        "spin_date_msk": today_key_msk(),
    }

    history = load_history()
    history.append(item)
    save_history(history)

    return {
        "ok": True,
        "prize_id": prize.get("id"),
        "prize_title": prize.get("title"),
        "prize_description": prize.get("description", ""),
        "code": code,
        "expires_at": expires_at.isoformat(),
        "expires_at_text": expires_at.strftime("%d.%m.%Y"),
    }


@app.post("/history")
async def history(request: Request):
    data = await request.json()
    init_data = data.get("init_data", "")

    user = get_user_from_init_data(init_data)
    if not user:
        return JSONResponse({"ok": False, "items": []}, status_code=401)

    items = load_user_history(str(user["id"]))
    return {"ok": True, "items": items}


async def run_bot():
    await dp.start_polling(bot)


def start_bot_in_thread():
    def runner():
        asyncio.run(run_bot())

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()


@app.on_event("startup")
async def on_startup():
    start_bot_in_thread()
