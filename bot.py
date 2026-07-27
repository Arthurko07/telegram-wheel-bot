import os
import json
import hmac
import hashlib
import random
import string
import asyncio
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEB_APP_URL = os.getenv("WEB_APP_URL")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
TRUSTED_IDS = [int(x) for x in os.getenv("TRUSTED_IDS", "").split(",") if x.strip()]

STORE_TIMEZONE = ZoneInfo("Europe/Moscow")
INIT_DATA_TTL = 60 * 60 * 24

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
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

class SpinRequest(BaseModel):
    init_data: str = ""

class AddPrizeStates(StatesGroup):
    title = State()
    description = State()
    weight = State()
    active = State()

class EditWeightStates(StatesGroup):
    prize_id = State()
    weight = State()

class DeletePrizeStates(StatesGroup):
    prize_id = State()

class TogglePrizeStates(StatesGroup):
    prize_id = State()

def load_prizes():
    if not os.path.exists(PRIZES_FILE):
        return []
    with open(PRIZES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_prizes(data):
    with open(PRIZES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_active_prizes():
    return [x for x in load_prizes() if x.get("active")]

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
    weights = [max(int(p.get("weight", 0)), 0) for p in prizes]
    return random.choices(prizes, weights=weights, k=1)[0]

def parse_dt(dt_str):
    return datetime.fromisoformat(dt_str)

def format_dt_msk(dt_obj):
    return dt_obj.astimezone(STORE_TIMEZONE).strftime("%d.%m.%Y %H:%M:%S МСК")

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

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

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

def is_code_expired(record: dict):
    expires_at = record.get("expires_at")
    if not expires_at:
        return False
    expires_dt = parse_dt(expires_at)
    now_utc = datetime.now(timezone.utc)
    return now_utc > expires_dt

def validate_init_data(init_data: str, bot_token: str):
    if not init_data or not bot_token:
        return None

    try:
        chunks = [
            chunk.split("=", 1)
            for chunk in unquote(init_data).split("&")
            if not chunk.startswith("hash=")
        ]
        chunks.sort(key=lambda x: x[0])
        data_check_string = "\n".join(f"{k}={v}" for k, v in chunks)

        hash_value = None
        for chunk in init_data.split("&"):
            if chunk.startswith("hash="):
                hash_value = chunk.split("=", 1)[1]
                break

        if not hash_value:
            return None

        data_map = {}
        for chunk in unquote(init_data).split("&"):
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                data_map[k] = v

        auth_date = data_map.get("auth_date")
        user_raw = data_map.get("user")

        if not auth_date or not user_raw:
            return None

        auth_date_int = int(auth_date)
        now_ts = int(datetime.now(timezone.utc).timestamp())
        if now_ts - auth_date_int > INIT_DATA_TTL:
            return None

        secret_key = hmac.new(
            b"WebAppData",
            bot_token.encode("utf-8"),
            hashlib.sha256
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, hash_value):
            return None

        user = json.loads(user_raw)
        if "id" not in user:
            return None

        return {
            "user_id": int(user["id"]),
            "username": user.get("username", ""),
            "first_name": user.get("first_name", "")
        }

    except Exception:
        return None

def admin_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="Список призов", callback_data="admin:list")
    builder.button(text="Добавить приз", callback_data="admin:add")
    builder.button(text="Изменить вес", callback_data="admin:edit_weight")
    builder.button(text="Вкл/выкл приз", callback_data="admin:toggle")
    builder.button(text="Удалить приз", callback_data="admin:delete")
    builder.adjust(1)
    return builder.as_markup()

def format_prizes_text():
    prizes = load_prizes()
    if not prizes:
        return "Список призов пуст."

    lines = ["Список призов:\n"]
    total_weight = sum(p.get("weight", 0) for p in prizes if p.get("active"))
    for p in prizes:
        active_text = "включен" if p.get("active") else "выключен"
        percent_text = ""
        if p.get("active") and total_weight > 0:
            chance = round((p.get("weight", 0) / total_weight) * 100, 2)
            percent_text = f" (~{chance}%)"
        lines.append(
            f"ID: {p.get('id')}\n"
            f"Название: {p.get('title')}\n"
            f"Описание: {p.get('description')}\n"
            f"Вес: {p.get('weight')}{percent_text}\n"
            f"Статус: {active_text}\n"
        )
    return "\n".join(lines)

def get_next_prize_id(prizes):
    if not prizes:
        return 1
    return max(p.get("id", 0) for p in prizes) + 1

def find_prize_by_id(prize_id: int):
    prizes = load_prizes()
    for prize in prizes:
        if prize.get("id") == prize_id:
            return prize
    return None

@app.get("/")
async def root():
    return {"ok": True, "service": "igadget-wheel-bot"}

@app.get("/prizes")
async def prizes_endpoint():
    prizes = get_active_prizes()
    result = []
    for idx, prize in enumerate(prizes):
        result.append({
            "id": prize.get("id", idx + 1),
            "title": prize.get("title", "Приз"),
            "description": prize.get("description", ""),
            "weight": prize.get("weight", 1)
        })
    return {"ok": True, "items": result}

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

    prizes = get_active_prizes()
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
            resp = {
                "ok": False,
                "error": "Вы уже крутили колесо сегодня. Следующая попытка будет доступна после 00:00 МСК.",
                "cooldown": True,
                "next_spin_at_text": format_next_spin_time_moscow(),
                "last_code": last_spin.get("code", ""),
                "last_prize_title": last_spin.get("prize_title", "")
            }
            if last_spin.get("expires_at"):
                resp["expires_at"] = last_spin["expires_at"]
                resp["expires_at_text"] = format_dt_msk(parse_dt(last_spin["expires_at"]))
            return resp

    prize = weighted_pick(prizes)
    code = generate_code()
    expires_at = now_utc + timedelta(days=7)

    record = {
        "user_id": user_id,
        "username": username,
        "first_name": first_name,
        "code": code,
        "prize_title": prize["title"],
        "prize_description": prize["description"],
        "created_at": now_utc.isoformat(),
        "expires_at": expires_at.isoformat(),
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
        f"Действителен до: {format_dt_msk(expires_at)}\n"
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
        "code": code,
        "expires_at": expires_at.isoformat(),
        "expires_at_text": format_dt_msk(expires_at)
    }

@dp.message(Command("start"))
async def start_cmd(message: Message):
    user_id = message.from_user.id

    if is_admin(user_id):
        await message.answer(
            "Откройте Mini App через кнопку меню Telegram.\n\n"
            "Команды владельца:\n"
            "/admin — управление призами\n"
            "/check КОД — проверить код\n"
            "/redeem КОД — погасить код"
        )
        return

    if is_staff(user_id):
        await message.answer(
            "Служебные команды:\n"
            "/check КОД — проверить код\n"
            "/redeem КОД — погасить код"
        )
        return

    await message.answer(
        "Откройте Mini App через кнопку меню Telegram и крутите колесо бонусов."
    )

@dp.message(Command("admin"))
async def admin_cmd(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("У вас нет доступа к админ-панели.")
        return
    await state.clear()
    await message.answer("Панель управления призами:", reply_markup=admin_menu())

@dp.callback_query(F.data == "admin:list")
async def admin_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.answer(format_prizes_text(), reply_markup=admin_menu())
    await callback.answer()

@dp.callback_query(F.data == "admin:add")
async def admin_add_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await state.set_state(AddPrizeStates.title)
    await callback.message.answer("Введите название нового приза:")
    await callback.answer()

@dp.message(AddPrizeStates.title)
async def admin_add_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AddPrizeStates.description)
    await message.answer("Введите описание приза:")

@dp.message(AddPrizeStates.description)
async def admin_add_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await state.set_state(AddPrizeStates.weight)
    await message.answer("Введите вес приза, например 25:")

@dp.message(AddPrizeStates.weight)
async def admin_add_weight(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("Вес должен быть целым числом. Введите ещё раз:")
        return

    await state.update_data(weight=int(text))
    await state.set_state(AddPrizeStates.active)
    await message.answer("Приз активен? Ответьте: да или нет")

@dp.message(AddPrizeStates.active)
async def admin_add_active(message: Message, state: FSMContext):
    text = message.text.strip().lower()
    if text not in ["да", "нет"]:
        await message.answer("Введите только: да или нет")
        return

    data = await state.get_data()
    prizes = load_prizes()

    new_prize = {
        "id": get_next_prize_id(prizes),
        "title": data["title"],
        "description": data["description"],
        "weight": data["weight"],
        "active": text == "да"
    }

    prizes.append(new_prize)
    save_prizes(prizes)
    await state.clear()

    await message.answer(
        f"Приз добавлен:\n"
        f"ID: {new_prize['id']}\n"
        f"Название: {new_prize['title']}\n"
        f"Вес: {new_prize['weight']}\n"
        f"Активен: {'да' if new_prize['active'] else 'нет'}",
        reply_markup=admin_menu()
    )

@dp.callback_query(F.data == "admin:edit_weight")
async def admin_edit_weight_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await state.set_state(EditWeightStates.prize_id)
    await callback.message.answer(format_prizes_text())
    await callback.message.answer("Введите ID приза, у которого нужно изменить вес:")
    await callback.answer()

@dp.message(EditWeightStates.prize_id)
async def admin_edit_weight_id(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("ID должен быть числом. Введите ещё раз:")
        return

    prize_id = int(text)
    prize = find_prize_by_id(prize_id)
    if not prize:
        await message.answer("Приз с таким ID не найден. Введите ещё раз:")
        return

    await state.update_data(prize_id=prize_id)
    await state.set_state(EditWeightStates.weight)
    await message.answer(f"Введите новый вес для приза «{prize['title']}»:")

@dp.message(EditWeightStates.weight)
async def admin_edit_weight_value(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("Вес должен быть числом. Введите ещё раз:")
        return

    data = await state.get_data()
    prize_id = data["prize_id"]
    new_weight = int(text)

    prizes = load_prizes()
    updated = None
    for prize in prizes:
        if prize.get("id") == prize_id:
            prize["weight"] = new_weight
            updated = prize
            break

    save_prizes(prizes)
    await state.clear()

    await message.answer(
        f"Вес обновлён.\n"
        f"ID: {updated['id']}\n"
        f"Название: {updated['title']}\n"
        f"Новый вес: {updated['weight']}",
        reply_markup=admin_menu()
    )

@dp.callback_query(F.data == "admin:toggle")
async def admin_toggle_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await state.set_state(TogglePrizeStates.prize_id)
    await callback.message.answer(format_prizes_text())
    await callback.message.answer("Введите ID приза, который нужно включить или выключить:")
    await callback.answer()

@dp.message(TogglePrizeStates.prize_id)
async def admin_toggle_finish(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("ID должен быть числом. Введите ещё раз:")
        return

    prize_id = int(text)
    prizes = load_prizes()
    updated = None

    for prize in prizes:
        if prize.get("id") == prize_id:
            prize["active"] = not prize.get("active", False)
            updated = prize
            break

    if not updated:
        await message.answer("Приз с таким ID не найден.")
        return

    save_prizes(prizes)
    await state.clear()

    await message.answer(
        f"Статус приза изменён.\n"
        f"ID: {updated['id']}\n"
        f"Название: {updated['title']}\n"
        f"Теперь: {'включен' if updated['active'] else 'выключен'}",
        reply_markup=admin_menu()
    )

@dp.callback_query(F.data == "admin:delete")
async def admin_delete_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await state.set_state(DeletePrizeStates.prize_id)
    await callback.message.answer(format_prizes_text())
    await callback.message.answer("Введите ID приза, который нужно удалить:")
    await callback.answer()

@dp.message(DeletePrizeStates.prize_id)
async def admin_delete_finish(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("ID должен быть числом. Введите ещё раз:")
        return

    prize_id = int(text)
    prizes = load_prizes()

    target = None
    new_prizes = []
    for prize in prizes:
        if prize.get("id") == prize_id:
            target = prize
        else:
            new_prizes.append(prize)

    if not target:
        await message.answer("Приз с таким ID не найден.")
        return

    save_prizes(new_prizes)
    await state.clear()

    await message.answer(
        f"Приз удалён.\n"
        f"ID: {target['id']}\n"
        f"Название: {target['title']}",
        reply_markup=admin_menu()
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

    expires_text = "—"
    expired_text = "Нет"
    if record.get("expires_at"):
        expires_dt = parse_dt(record["expires_at"])
        expires_text = format_dt_msk(expires_dt)
        expired_text = "Да" if is_code_expired(record) else "Нет"

    text = (
        f"Проверка кода\n"
        f"Код: {record.get('code', '—')}\n"
        f"Приз: {record.get('prize_title', '—')}\n"
        f"Имя: {record.get('first_name') or 'Без имени'}\n"
        f"Username: {username_value}\n"
        f"Использован: {redeemed_text}\n"
        f"Действителен до: {expires_text}\n"
        f"Просрочен: {expired_text}\n"
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

    if is_code_expired(record):
        expires_text = format_dt_msk(parse_dt(record["expires_at"])) if record.get("expires_at") else "—"
        await message.answer(
            f"Этот код просрочен и не может быть погашен.\n"
            f"Код: {record.get('code', '—')}\n"
            f"Приз: {record.get('prize_title', '—')}\n"
            f"Истёк: {expires_text}"
        )
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

def main():
    async def runner():
        bot_task = asyncio.create_task(dp.start_polling(bot))
        config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
        server = uvicorn.Server(config)
        await server.serve()
        await bot_task

    asyncio.run(runner())

if __name__ == "__main__":
    main()
