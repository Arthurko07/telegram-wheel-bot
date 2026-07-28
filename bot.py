import os
import json
import hmac
import hashlib
import random
import string
import urllib.parse
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    WebAppInfo,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Update,
)
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
BACKEND_URL = os.getenv("BACKEND_URL", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "igadget-wheel-secret").strip()
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

PRIZES_FILE = "prizes.json"
USED_CODES_FILE = "used_codes.json"
HISTORY_FILE = "history.json"
WEBHOOK_PATH = "/telegram-webhook"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


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


def normalize_weight(value, default=1.0):
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return float(default)
    if weight <= 0:
        return float(default)
    return round(weight, 3)


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
    if not received_hash:
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
    items = read_json_file(PRIZES_FILE, [])
    if not isinstance(items, list):
        return []
    normalized = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        normalized.append({
            "id": int(item.get("id", index)),
            "title": str(item.get("title", "")).strip(),
            "short": str(item.get("short", "")).strip(),
            "description": str(item.get("description", "")).strip(),
            "weight": normalize_weight(item.get("weight", 1)),
            "active": bool(item.get("active", True)),
        })
    return normalized


def enrich_prizes_with_probability(items):
    prizes = [dict(item) for item in (items or []) if isinstance(item, dict)]
    active_items = [x for x in prizes if x.get("active", True)]
    total_weight = sum(normalize_weight(x.get("weight", 1)) for x in active_items)

    for item in prizes:
        item["weight"] = normalize_weight(item.get("weight", 1))
        if item.get("active", True) and total_weight > 0:
            item["drop_percent"] = round((item["weight"] / total_weight) * 100, 2)
        else:
            item["drop_percent"] = 0.0
    return prizes


def save_prizes(items):
    normalized = []
    for index, item in enumerate(items or [], start=1):
        if not isinstance(item, dict):
            continue
        normalized.append({
            "id": int(item.get("id", index)),
            "title": str(item.get("title", "")).strip(),
            "short": str(item.get("short", "")).strip(),
            "description": str(item.get("description", "")).strip(),
            "weight": normalize_weight(item.get("weight", 1)),
            "active": bool(item.get("active", True)),
        })
    write_json_file(PRIZES_FILE, normalized)


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

    raise RuntimeError("Could not generate unique code")


def choose_weighted_prize(prizes):
    active_prizes = [p for p in prizes if p.get("active", True)]
    if not active_prizes:
        return None

    total_weight = sum(normalize_weight(p.get("weight", 1)) for p in active_prizes)
    if total_weight <= 0:
        return None

    threshold = random.uniform(0, total_weight)
    cumulative = 0.0

    for prize in active_prizes:
        cumulative += normalize_weight(prize.get("weight", 1))
        if threshold <= cumulative:
            return prize

    return active_prizes[-1]


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
async def bonus_text_handler(message: Message):
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    webhook_url = f"{BACKEND_URL.rstrip('/')}{WEBHOOK_PATH}" if BACKEND_URL else ""

    if webhook_url.startswith("https://"):
        await bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True,
        )
    yield
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    finally:
        await bot.session.close()


app = FastAPI(title="iGadget Wheel API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"ok": True, "message": "Wheel API is running"}


@app.get("/health")
def health():
    return {
        "ok": True,
        "bot": True,
        "webapp_url_set": bool(WEBAPP_URL),
        "backend_url_set": bool(BACKEND_URL),
    }


@app.get("/prizes")
def prizes():
    items = load_prizes()
    active = [x for x in items if x.get("active", True)]
    public_items = []

    for item in active:
        public_items.append({
            "id": item.get("id"),
            "title": item.get("title"),
            "short": item.get("short"),
            "description": item.get("description", ""),
            "active": bool(item.get("active", True)),
        })

    return {"ok": True, "items": public_items}


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
        "prize_weight": normalize_weight(prize.get("weight", 1)),
        "prize_drop_percent": next((x.get("drop_percent", 0.0) for x in enrich_prizes_with_probability(prizes_data) if int(x.get("id", 0)) == int(prize.get("id", 0))), 0.0),
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
        "prize_weight": item["prize_weight"],
        "prize_drop_percent": item["prize_drop_percent"],
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


@app.post("/admin/me")
async def admin_me(request: Request):
    data = await request.json()
    init_data = data.get("init_data", "")

    user = get_user_from_init_data(init_data)
    if not is_admin(user):
        return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)

    return {
        "ok": True,
        "telegram_id": user["id"],
        "first_name": user.get("first_name"),
        "username": user.get("username"),
    }


@app.post("/admin/prizes")
async def admin_prizes(request: Request):
    data = await request.json()
    init_data = data.get("init_data", "")

    user = get_user_from_init_data(init_data)
    if not is_admin(user):
        return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)

    items = enrich_prizes_with_probability(load_prizes())
    items.sort(key=lambda x: int(x.get("id", 0)))
    return {"ok": True, "items": items}


@app.post("/admin/prize/add")
async def admin_prize_add(request: Request):
    data = await request.json()
    init_data = data.get("init_data", "")

    user = get_user_from_init_data(init_data)
    if not is_admin(user):
        return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)

    prizes_data = load_prizes()
    next_id = max([int(x.get("id", 0)) for x in prizes_data], default=0) + 1

    item = {
        "id": next_id,
        "title": str(data.get("title", "")).strip(),
        "short": str(data.get("short", "")).strip(),
        "description": str(data.get("description", "")).strip(),
        "weight": normalize_weight(data.get("weight", 1)),
        "active": bool(data.get("active", True)),
    }

    if not item["title"] or not item["short"] or not item["description"]:
        return JSONResponse({"ok": False, "error": "Заполните все поля"}, status_code=400)

    prizes_data.append(item)
    save_prizes(prizes_data)
    items = enrich_prizes_with_probability(load_prizes())
    items.sort(key=lambda x: int(x.get("id", 0)))
    return {"ok": True, "items": items}


@app.post("/admin/prize/update")
async def admin_prize_update(request: Request):
    data = await request.json()
    init_data = data.get("init_data", "")

    user = get_user_from_init_data(init_data)
    if not is_admin(user):
        return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)

    try:
        prize_id = int(data.get("prize_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Некорректный prize_id"}, status_code=400)

    prizes_data = load_prizes()
    target = next((x for x in prizes_data if int(x.get("id")) == prize_id), None)
    if not target:
        return JSONResponse({"ok": False, "error": "Приз не найден"}, status_code=404)

    target["title"] = str(data.get("title", "")).strip()
    target["short"] = str(data.get("short", "")).strip()
    target["description"] = str(data.get("description", "")).strip()
    target["weight"] = normalize_weight(data.get("weight", 1))
    target["active"] = bool(data.get("active", True))

    if not target["title"] or not target["short"] or not target["description"]:
        return JSONResponse({"ok": False, "error": "Заполните все поля"}, status_code=400)

    save_prizes(prizes_data)
    items = enrich_prizes_with_probability(load_prizes())
    items.sort(key=lambda x: int(x.get("id", 0)))
    return {"ok": True, "items": items}


@app.post("/admin/prize/update-weight")
async def admin_prize_update_weight(request: Request):
    data = await request.json()
    init_data = data.get("init_data", "")

    user = get_user_from_init_data(init_data)
    if not is_admin(user):
        return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)

    try:
        prize_id = int(data.get("prize_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Некорректный prize_id"}, status_code=400)

    weight = normalize_weight(data.get("weight", 1))
    prizes_data = load_prizes()
    target = next((x for x in prizes_data if int(x.get("id")) == prize_id), None)
    if not target:
        return JSONResponse({"ok": False, "error": "Приз не найден"}, status_code=404)

    target["weight"] = weight
    save_prizes(prizes_data)
    items = enrich_prizes_with_probability(load_prizes())
    items.sort(key=lambda x: int(x.get("id", 0)))
    return {"ok": True, "items": items}


@app.post("/admin/prize/toggle")
async def admin_prize_toggle(request: Request):
    data = await request.json()
    init_data = data.get("init_data", "")

    user = get_user_from_init_data(init_data)
    if not is_admin(user):
        return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)

    try:
        prize_id = int(data.get("prize_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Некорректный prize_id"}, status_code=400)

    prizes_data = load_prizes()
    target = next((x for x in prizes_data if int(x.get("id")) == prize_id), None)
    if not target:
        return JSONResponse({"ok": False, "error": "Приз не найден"}, status_code=404)

    target["active"] = not bool(target.get("active", True))
    save_prizes(prizes_data)
    items = enrich_prizes_with_probability(load_prizes())
    items.sort(key=lambda x: int(x.get("id", 0)))
    return {"ok": True, "items": items}


@app.post("/admin/prize/delete")
async def admin_prize_delete(request: Request):
    data = await request.json()
    init_data = data.get("init_data", "")

    user = get_user_from_init_data(init_data)
    if not is_admin(user):
        return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)

    try:
        prize_id = int(data.get("prize_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Некорректный prize_id"}, status_code=400)

    history_data = load_history()
    linked = next((x for x in history_data if int(x.get("prize_id", 0)) == prize_id), None)
    if linked:
        return JSONResponse(
            {"ok": False, "error": "Нельзя удалить приз, который уже есть в истории"},
            status_code=400
        )

    prizes_data = load_prizes()
    prizes_data = [x for x in prizes_data if int(x.get("id")) != prize_id]
    save_prizes(prizes_data)
    items = enrich_prizes_with_probability(load_prizes())
    items.sort(key=lambda x: int(x.get("id", 0)))
    return {"ok": True, "items": items}


@app.post("/admin/code/check")
async def admin_code_check(request: Request):
    data = await request.json()
    init_data = data.get("init_data", "")
    code = str(data.get("code", "")).strip()

    user = get_user_from_init_data(init_data)
    if not is_admin(user):
        return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)

    history_data = load_history()
    item = next((x for x in history_data if x.get("code") == code), None)
    if not item:
        return {"ok": False, "error": "Код не найден"}

    return {
        "ok": True,
        "code": item.get("code"),
        "redeemed": item.get("redeemed", False),
        "prize_title": item.get("prize_title", "Приз"),
        "prize_weight": item.get("prize_weight"),
        "prize_drop_percent": item.get("prize_drop_percent"),
    }


@app.post("/admin/code/redeem")
async def admin_code_redeem(request: Request):
    data = await request.json()
    init_data = data.get("init_data", "")
    code = str(data.get("code", "")).strip()

    user = get_user_from_init_data(init_data)
    if not is_admin(user):
        return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)

    history_data = load_history()
    item = next((x for x in history_data if x.get("code") == code), None)
    if not item:
        return {"ok": False, "error": "Код не найден"}

    if item.get("redeemed"):
        return {
            "ok": False,
            "error": "Код уже погашен",
            "code": item.get("code"),
            "prize_title": item.get("prize_title", "Приз"),
        }

    item["redeemed"] = True
    save_history(history_data)

    return {
        "ok": True,
        "code": item.get("code"),
        "prize_title": item.get("prize_title", "Приз"),
    }


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}
