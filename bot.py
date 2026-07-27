import os
import json
import hmac
import hashlib
import random
import string
import asyncio
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEB_APP_URL = os.getenv("WEB_APP_URL")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
TRUSTED_IDS = [int(x) for x in os.getenv("TRUSTED_IDS", "").split(",") if x.strip()]

STORE_TIMEZONE = ZoneInfo("Europe/Moscow")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

USED_CODES_FILE = "used_codes.json"
PRIZES_FILE = "prizes.json"

def load_prizes():
    with open(PRIZES_FILE, "r", encoding="utf-8") as f:
        items = json.load(f)
    return [x for x in items if x.get("active")]

def load_used_codes():
    if not os.path.exists(USED_CODES_FILE):
        return []
    with open(USED_CODES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_used_codes(data):
    with open(USED_CODES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def generate_code():
    used = load_used_codes()
    existing = {x["code"] for x in used if "code" in x}

    while True:
        code = "IG-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if code not in existing:
            return code

def weighted_pick(prizes):
    weights = [p["weight"] for p in prizes]
    return random.choices(prizes, weights=weights, k=1)[0]

def parse_dt(dt_str):
    return datetime.fromisoformat(dt_str)

def format_next_spin_time_moscow():
    now_msk = datetime.now(timezone.utc).astimezone(STORE_TIMEZONE)
    tomorrow = now_msk.date().fromordinal(now_msk.date().toordinal() + 1)
    next_spin = datetime(
        tomorrow.year,
        tomorrow.month,
        tomorrow.day,
        0, 0, 0,
        tzinfo=STORE_TIMEZONE
    )
    return next_spin.strftime("%d.%m.%Y %H:%M МСК")

def is_staff(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in TRUSTED_IDS

def find_code_record(code: str, used: list):
    code = code.strip().upper()
    for item in used:
        if item.get("code", "").upper() == code:
            return item
    return None

def find_last_spin_by_user_id(user_id: int, used: list):
    records = [x for x in used if x.get("user_id") == user_id and x.get("created_at")]
    if not records:
        return None
    records.sort(key=lambda x: x["created_at"], reverse=True)
    return records[0]

def validate_init_data(init_data: str, bot_token: str):
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(
        b"WebAppData",
        bot_token.encode(),
        hashlib.sha256
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        return None

    user_raw = parsed.get("user")
    if not user_raw:
        return None

    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        return None

    if "id" not in user:
        return None

    return {
        "user_id": int(user["id"]),
        "username": user.get("username", ""),
        "first_name": user.get("first_name", "")
    }

class SpinRequest(BaseModel):
    init_data: str = ""

@dp.message(Command("start"))
async def start_cmd(message: Message):
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Открыть колесо", web_app=WebAppInfo(url=WEB_APP_URL))]
        ],
        resize_keyboard=True
    )
    await message.answer(
        "Нажмите кнопку ниже, чтобы открыть колесо бонусов.\n\n"
        "Для сотрудников:\n"
        "/check КОД — проверить код\n"
        "/redeem КОД — погасить код",
        reply_markup=kb
    )

@dp.message(Command("check"))
async def check_code_cmd(message: Message):
    if not is_staff(message.from_user.id):
        await message.answer("У вас нет доступа к этой команде.")
        return

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /check IG-XXXXXX")
        return

    code = parts[1].strip().upper()
    used = load_used_codes()
    record = find_code_record(code, used)

    if not record:
        await message.answer("Код не найден.")
        return

    redeemed = record.get("redeemed", False)
    redeemed_text = "Да" if redeemed else "Нет"
    username_value = f"@{record.get('username')}" if record.get("username") else "без username"

    text = (
        f"Проверка кода\n"
        f"Код: {record.get('code', '—')}\n"
        f"Приз: {record.get('prize_title', '—')}\n"
        f"Имя: {record.get('first_name') or 'Без имени'}\n"
        f"Username: {username_value}\n"
        f"Использован: {redeemed_text}\n"
        f"Выдан: {record.get('created_at', '—')}"
    )

    if redeemed:
        text += (
            f"\nПогашен: {record.get('redeemed_at', '—')}\n"
            f"Кем: {record.get('redeemed_by', '—')}"
        )

    await message.answer(text)

@dp.message(Command("redeem"))
async def redeem_code_cmd(message: Message):
    if not is_staff(message.from_user.id):
        await message.answer("У вас нет доступа к этой команде.")
        return

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /redeem IG-XXXXXX")
        return

    code = parts[1].strip().upper()
    used = load_used_codes()
    record = find_code_record(code, used)

    if not record:
        await message.answer("Код не найден.")
        return

    if record.get("redeemed", False):
        await message.answer(
            f"Этот код уже использован.\n"
            f"Код: {record.get('code', '—')}\n"
            f"Погашен: {record.get('redeemed_at', '—')}\n"
            f"Кем: {record.get('redeemed_by', '—')}"
        )
        return

    now_utc = datetime.now(timezone.utc)
    now_msk = now_utc.astimezone(STORE_TIMEZONE)
    staff_name = message.from_user.full_name or "Сотрудник"
    staff_username = f"@{message.from_user.username}" if message.from_user.username else "без username"

    record["redeemed"] = True
    record["redeemed_at"] = now_utc.isoformat()
    record["redeemed_by"] = f"{staff_name} ({staff_username}, id={message.from_user.id})"

    save_used_codes(used)

    await message.answer(
        f"Код погашен.\n"
        f"Код: {record.get('code', '—')}\n"
        f"Приз: {record.get('prize_title', '—')}"
    )

    client_username = f"@{record.get('username')}" if record.get("username") else "без username"
    notify_text = (
        f"✅ Код погашен\n"
        f"Код: {record.get('code', '—')}\n"
        f"Приз: {record.get('prize_title', '—')}\n"
        f"Клиент: {record.get('first_name') or 'Без имени'}\n"
        f"Username клиента: {client_username}\n"
        f"Погасил: {staff_name} ({staff_username})\n"
        f"Время МСК: {now_msk.strftime('%d.%m.%Y %H:%M:%S')}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, notify_text)
        except Exception:
            pass

@app.get("/")
async def root():
    return {"ok": True, "service": "igadget-wheel-bot"}

@app.post("/spin")
async def spin(req: SpinRequest):
    auth = validate_init_data(req.init_data, BOT_TOKEN)

    if not auth:
        return {
            "ok": False,
            "error": "Не удалось проверить Telegram-пользователя"
        }

    user_id = auth["user_id"]
    username = auth["username"]
    first_name = auth["first_name"]

    prizes = load_prizes()
    if not prizes:
        return {"ok": False, "error": "Нет активных призов"}

    used = load_used_codes()
    last_spin = find_last_spin_by_user_id(user_id, used)

    now_utc = datetime.now(timezone.utc)
    now_msk = now_utc.astimezone(STORE_TIMEZONE)

    if last_spin:
        last_spin_time = parse_dt(last_spin["created_at"])
        last_spin_msk = last_spin_time.astimezone(STORE_TIMEZONE)

        if last_spin_msk.date() == now_msk.date():
            return {
                "ok": False,
                "error": "Вы уже крутили колесо сегодня. Следующая попытка будет доступна после 00:00 МСК.",
                "cooldown": True,
                "next_spin_at_text": format_next_spin_time_moscow(),
                "last_code": last_spin.get("code", ""),
                "last_prize_title": last_spin.get("prize_title", "")
            }

    prize = weighted_pick(prizes)
    code = generate_code()

    record = {
        "user_id": user_id,
        "username": username,
        "first_name": first_name,
        "code": code,
        "prize_title": prize["title"],
        "prize_description": prize["description"],
        "created_at": now_utc.isoformat(),
        "redeemed": False,
        "redeemed_at": None,
        "redeemed_by": None
    }

    used.append(record)
    save_used_codes(used)

    username_part = f"@{username}" if username else "без username"
    first_name_part = first_name if first_name else "Без имени"

    text = (
        f"🎁 Новый выигрыш\n"
        f"Имя: {first_name_part}\n"
        f"Username: {username_part}\n"
        f"User ID: {user_id}\n"
        f"Приз: {prize['title']}\n"
        f"Описание: {prize['description']}\n"
        f"Код: {code}\n"
        f"Время UTC: {record['created_at']}\n"
        f"Время МСК: {now_msk.strftime('%d.%m.%Y %H:%M:%S')}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass

    return {
        "ok": True,
        "prize_title": prize["title"],
        "prize_description": prize["description"],
        "code": code
    }

async def run_bot():
    await dp.start_polling(bot)

def main():
    loop = asyncio.get_event_loop()
    loop.create_task(run_bot())

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000"))
    )
    server = uvicorn.Server(config)
    loop.run_until_complete(server.serve())

if __name__ == "__main__":
    main()
