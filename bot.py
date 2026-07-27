import os
import json
import random
import string
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import asyncio

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

def get_identity_key(req):
    if req.user_id is not None:
        return f"user_id:{req.user_id}"
    if req.username:
        return f"username:{req.username.lower()}"
    if req.session_id:
        return f"session:{req.session_id}"
    return None

def find_last_spin_by_identity(identity_key, used):
    if not identity_key:
        return None

    records = [x for x in used if x.get("identity_key") == identity_key and x.get("created_at")]
    if not records:
        return None

    records.sort(key=lambda x: x["created_at"], reverse=True)
    return records[0]

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

class SpinRequest(BaseModel):
    user_id: int | None = None
    username: str = ""
    first_name: str = ""
    session_id: str = ""

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

    text = (
        f"Проверка кода\n"
        f"Код: {record.get('code', '—')}\n"
        f"Приз: {record.get('prize_title', '—')}\n"
        f"Имя: {record.get('first_name') or 'Без имени'}\n"
        f"Username: @{record.get('username')}" if record.get('username') else
        f"Проверка кода\nКод: {record.get('code', '—')}\nПриз: {record.get('prize_title', '—')}\nИмя: {record.get('first_name') or 'Без имени'}\nUsername: без username"
    )

    extra = (
        f"\nИспользован: {redeemed_text}\n"
        f"Выдан: {record.get('created_at', '—')}"
    )

    if redeemed:
        extra += (
            f"\nПогашен: {record.get('redeemed_at', '—')}\n"
            f"Кем: {record.get('redeemed_by', '—')}"
        )

    await message.answer(text + extra)

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

    notify_text = (
        f"✅ Код погашен\n"
        f"Код: {record.get('code', '—')}\n"
        f"Приз: {record.get('prize_title', '—')}\n"
        f"Клиент: {record.get('first_name') or 'Без имени'}\n"
        f"Username клиента: @{record.get('username')}" if record.get('username') else
        f"✅ Код погашен\nКод: {record.get('code', '—')}\nПриз: {record.get('prize_title', '—')}\nКлиент: {record.get('first_name') or 'Без имени'}\nUsername клиента: без username"
    )

    notify_tail = (
        f"\nПогасил: {staff_name} ({staff_username})\n"
        f"Время МСК: {now_msk.strftime('%d.%m.%Y %H:%M:%S')}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, notify_text + notify_tail)
        except Exception:
            pass

@app.get("/")
async def root():
    return {"ok": True, "service": "igadget-wheel-bot"}

@app.post("/spin")
async def spin(req: SpinRequest):
    identity_key = get_identity_key(req)

    if not identity_key:
        return {
            "ok": False,
            "error": "Не удалось определить пользователя"
        }

    prizes = load_prizes()
    if not prizes:
        return {"ok": False, "error": "Нет активных призов"}

    used = load_used_codes()
    last_spin = find_last_spin_by_identity(identity_key, used)

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
        "identity_key": identity_key,
        "code": code,
        "user_id": req.user_id,
        "username": req.username,
        "first_name": req.first_name,
        "session_id": req.session_id,
        "prize_title": prize["title"],
        "prize_description": prize["description"],
        "created_at": now_utc.isoformat(),
        "redeemed": False,
        "redeemed_at": None,
        "redeemed_by": None
    }

    used.append(record)
    save_used_codes(used)

    username_part = f"@{req.username}" if req.username else "без username"
    first_name_part = req.first_name if req.first_name else "Без имени"
    user_id_part = req.user_id if req.user_id is not None else "не передан"

    text = (
        f"🎁 Новый выигрыш\n"
        f"Имя: {first_name_part}\n"
        f"Username: {username_part}\n"
        f"User ID: {user_id_part}\n"
        f"Session ID: {req.session_id or 'нет'}\n"
        f"Identity: {identity_key}\n"
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
