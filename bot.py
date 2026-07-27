import os
import json
import random
import string
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn
import asyncio

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEB_APP_URL = os.getenv("WEB_APP_URL")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()

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

class SpinRequest(BaseModel):
    user_id: int | None = None
    username: str = ""
    first_name: str = ""

@dp.message(Command("start"))
async def start_cmd(message: Message):
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Открыть колесо", web_app=WebAppInfo(url=WEB_APP_URL))]],
        resize_keyboard=True
    )
    await message.answer("Нажмите кнопку ниже, чтобы открыть колесо бонусов.", reply_markup=kb)

@app.get("/")
async def root():
    return {"ok": True, "service": "igadget-wheel-bot"}

@app.post("/spin")
async def spin(req: SpinRequest):
    prizes = load_prizes()
    if not prizes:
        return {"ok": False, "error": "Нет активных призов"}

    prize = weighted_pick(prizes)
    code = generate_code()

    record = {
        "code": code,
        "user_id": req.user_id,
        "username": req.username,
        "first_name": req.first_name,
        "prize_title": prize["title"],
        "prize_description": prize["description"],
        "created_at": datetime.utcnow().isoformat()
    }

    used = load_used_codes()
    used.append(record)
    save_used_codes(used)

    username_part = f"@{req.username}" if req.username else "без username"
    text = (
        f"🎁 Новый выигрыш\n"
        f"Имя: {req.first_name}\n"
        f"Username: {username_part}\n"
        f"User ID: {req.user_id}\n"
        f"Приз: {prize['title']}\n"
        f"Код: {code}\n"
        f"Время: {record['created_at']}"
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
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
    server = uvicorn.Server(config)
    loop.run_until_complete(server.serve())

if __name__ == "__main__":
    main()
