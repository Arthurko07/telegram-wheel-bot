import os
import json
import hmac
import hashlib
import random
import string
import urllib.parse
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import dict_row

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    Update,
    MenuButtonWebApp,
    WebAppInfo,
)
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
BACKEND_URL = os.getenv("BACKEND_URL", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "igadget-wheel-secret").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

WEBHOOK_PATH = "/telegram-webhook"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def db_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def normalize_weight(value, default=1.0):
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return float(default)
    if weight <= 0:
        return float(default)
    return round(weight, 3)


def normalize_image_url(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return ""


def init_db():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS prizes (
                    id BIGSERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    short TEXT NOT NULL,
                    description TEXT NOT NULL,
                    image_url TEXT NOT NULL DEFAULT '',
                    weight DOUBLE PRECISION NOT NULL DEFAULT 1,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS used_codes (
                    code TEXT PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS spin_history (
                    id BIGINT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    telegram_id TEXT NOT NULL,
                    first_name TEXT,
                    username TEXT,
                    prize_id BIGINT NOT NULL,
                    prize_title TEXT NOT NULL,
                    prize_description TEXT NOT NULL DEFAULT '',
                    prize_image_url TEXT NOT NULL DEFAULT '',
                    prize_weight DOUBLE PRECISION NOT NULL DEFAULT 1,
                    prize_drop_percent DOUBLE PRECISION NOT NULL DEFAULT 0,
                    code TEXT NOT NULL UNIQUE,
                    redeemed BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL,
                    created_at_text TEXT NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    expires_at_text TEXT NOT NULL,
                    spin_date_msk TEXT NOT NULL
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_spin_history_user_id
                ON spin_history (user_id)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_spin_history_spin_date_msk
                ON spin_history (spin_date_msk)
            """)

            cur.execute("SELECT COUNT(*) AS count FROM prizes")
            count = cur.fetchone()["count"]

            if count == 0:
                cur.executemany("""
                    INSERT INTO prizes (title, short, description, image_url, weight, active)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, [
                    ("Скидка 5% на аксессуары", "-5%", "Скидка 5% на аксессуары.", "", 35, True),
                    ("Скидка 10% на аксессуары", "-10%", "Скидка 10% на аксессуары.", "", 22, True),
                    ("Скидка 15% на аксессуары", "-15%", "Скидка 15% на аксессуары.", "", 12, True),
                    ("Бесплатная доставка", "Дост.", "Бесплатная доставка на заказ.", "", 14, True),
                    ("Подарок к покупке", "Подарок", "Небольшой подарок при следующей покупке.", "", 10, True),
                    ("Бонус 500", "500", "500 бонусов на следующий заказ.", "", 7, True),
                ])
        conn.commit()


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
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, short, description, image_url, weight, active
                FROM prizes
                ORDER BY id ASC
            """)
            items = cur.fetchall()

    normalized = []
    for index, item in enumerate(items, start=1):
        normalized.append({
            "id": int(item.get("id", index)),
            "title": str(item.get("title", "")).strip(),
            "short": str(item.get("short", "")).strip(),
            "description": str(item.get("description", "")).strip(),
            "image_url": normalize_image_url(item.get("image_url", "")),
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
        item["image_url"] = normalize_image_url(item.get("image_url", ""))
        if item.get("active", True) and total_weight > 0:
            item["drop_percent"] = round((item["weight"] / total_weight) * 100, 2)
        else:
            item["drop_percent"] = 0.0
    return prizes


def save_prize(item):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO prizes (title, short, description, image_url, weight, active)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                str(item.get("title", "")).strip(),
                str(item.get("short", "")).strip(),
                str(item.get("description", "")).strip(),
                normalize_image_url(item.get("image_url", "")),
                normalize_weight(item.get("weight", 1)),
                bool(item.get("active", True)),
            ))
            row = cur.fetchone()
        conn.commit()
    return int(row["id"])


def update_prize(prize_id, item):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE prizes
                SET title = %s,
                    short = %s,
                    description = %s,
                    image_url = %s,
                    weight = %s,
                    active = %s
                WHERE id = %s
            """, (
                str(item.get("title", "")).strip(),
                str(item.get("short", "")).strip(),
                str(item.get("description", "")).strip(),
                normalize_image_url(item.get("image_url", "")),
                normalize_weight(item.get("weight", 1)),
                bool(item.get("active", True)),
                int(prize_id),
            ))
        conn.commit()


def update_prize_weight(prize_id, weight):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE prizes
                SET weight = %s
                WHERE id = %s
            """, (normalize_weight(weight), int(prize_id)))
        conn.commit()


def toggle_prize(prize_id):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE prizes
                SET active = NOT active
                WHERE id = %s
                RETURNING id
            """, (int(prize_id),))
            row = cur.fetchone()
        conn.commit()
    return row is not None


def delete_prize(prize_id):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM prizes WHERE id = %s", (int(prize_id),))
        conn.commit()


def prize_exists(prize_id):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM prizes WHERE id = %s", (int(prize_id),))
            return cur.fetchone() is not None


def load_used_code(code):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT code FROM used_codes WHERE code = %s", (str(code),))
            return cur.fetchone() is not None


def save_used_code(code):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO used_codes (code)
                VALUES (%s)
                ON CONFLICT (code) DO NOTHING
            """, (str(code),))
        conn.commit()


def load_history():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT *
                FROM spin_history
                ORDER BY created_at DESC
            """)
            return cur.fetchall()


def save_history_item(item):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO spin_history (
                    id, user_id, telegram_id, first_name, username,
                    prize_id, prize_title, prize_description, prize_image_url,
                    prize_weight, prize_drop_percent, code, redeemed,
                    created_at, created_at_text, expires_at, expires_at_text, spin_date_msk
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
            """, (
                int(item["id"]),
                str(item["user_id"]),
                str(item["telegram_id"]),
                item.get("first_name"),
                item.get("username"),
                int(item["prize_id"]),
                str(item["prize_title"]),
                str(item.get("prize_description", "")),
                str(item.get("prize_image_url", "")),
                float(item["prize_weight"]),
                float(item["prize_drop_percent"]),
                str(item["code"]),
                bool(item["redeemed"]),
                item["created_at"],
                str(item["created_at_text"]),
                item["expires_at"],
                str(item["expires_at_text"]),
                str(item["spin_date_msk"]),
            ))
        conn.commit()


def load_user_history(user_id):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT *
                FROM spin_history
                WHERE user_id = %s
                ORDER BY created_at DESC
            """, (str(user_id),))
            return cur.fetchall()


def generate_code(length=6):
    chars = string.ascii_uppercase + string.digits
    return "IG-" + "".join(random.choice(chars) for _ in range(length))


def generate_unique_code():
    for _ in range(50):
        code = generate_code()
        if not load_used_code(code):
            save_used_code(code)
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
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT *
                FROM spin_history
                WHERE user_id = %s AND spin_date_msk = %s
                ORDER BY created_at DESC
                LIMIT 1
            """, (str(user_id), today_key_msk()))
            return cur.fetchone()


def is_admin(user):
    if not user:
        return False
    try:
        return int(user["id"]) in ADMIN_IDS
    except Exception:
        return False


async def set_default_menu_button():
    if not WEBAPP_URL:
        print("[startup] WEBAPP_URL is empty, menu button skipped")
        return

    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Бонусы iGadget",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )
        )
        print("[startup] menu button set successfully")
    except Exception as e:
        print(f"[startup] menu button set error: {e}")


@dp.message(CommandStart())
async def start_handler(message: Message):
    if not WEBAPP_URL:
        await message.answer("Не задан WEBAPP_URL в переменных окружения.")
        return

    await message.answer(
        "Добро пожаловать в iGadget Wheel.\n"
        "Откройте колесо через кнопку «Бонусы iGadget» внизу экрана."
    )


@dp.message(F.text == "Бонусы iGadget")
async def bonus_text_handler(message: Message):
    await message.answer(
        "Используйте нижнюю кнопку «Бонусы iGadget» в меню Telegram."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[startup] init_db...")
    init_db()
    print("[startup] database ready")

    await set_default_menu_button()

    webhook_url = f"{BACKEND_URL.rstrip('/')}{WEBHOOK_PATH}" if BACKEND_URL else ""
    print(f"[startup] BACKEND_URL={BACKEND_URL}")
    print(f"[startup] webhook_url={webhook_url}")

    if webhook_url.startswith("https://"):
        try:
            await bot.set_webhook(
                url=webhook_url,
                secret_token=WEBHOOK_SECRET,
                drop_pending_updates=True,
            )
            print("[startup] webhook set successfully")
        except Exception as e:
            print(f"[startup] webhook set error: {e}")

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
        "database_url_set": bool(DATABASE_URL),
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
            "image_url": item.get("image_url", ""),
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
            "last_prize_image_url": existing_today.get("prize_image_url", ""),
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
        "prize_image_url": prize.get("image_url", ""),
        "prize_weight": normalize_weight(prize.get("weight", 1)),
        "prize_drop_percent": next(
            (
                x.get("drop_percent", 0.0)
                for x in enrich_prizes_with_probability(prizes_data)
                if int(x.get("id", 0)) == int(prize.get("id", 0))
            ),
            0.0
        ),
        "code": code,
        "redeemed": False,
        "created_at": created_at,
        "created_at_text": created_at.strftime("%d.%m.%Y %H:%M"),
        "expires_at": expires_at,
        "expires_at_text": expires_at.strftime("%d.%m.%Y"),
        "spin_date_msk": today_key_msk(),
    }

    save_history_item(item)

    return {
        "ok": True,
        "prize_id": prize.get("id"),
        "prize_title": prize.get("title"),
        "prize_description": prize.get("description", ""),
        "prize_image_url": prize.get("image_url", ""),
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

    item = {
        "title": str(data.get("title", "")).strip(),
        "short": str(data.get("short", "")).strip(),
        "description": str(data.get("description", "")).strip(),
        "image_url": normalize_image_url(data.get("image_url", "")),
        "weight": normalize_weight(data.get("weight", 1)),
        "active": bool(data.get("active", True)),
    }

    if not item["title"] or not item["short"] or not item["description"]:
        return JSONResponse({"ok": False, "error": "Заполните все поля"}, status_code=400)

    save_prize(item)
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

    if not prize_exists(prize_id):
        return JSONResponse({"ok": False, "error": "Приз не найден"}, status_code=404)

    item = {
        "title": str(data.get("title", "")).strip(),
        "short": str(data.get("short", "")).strip(),
        "description": str(data.get("description", "")).strip(),
        "image_url": normalize_image_url(data.get("image_url", "")),
        "weight": normalize_weight(data.get("weight", 1)),
        "active": bool(data.get("active", True)),
    }

    if not item["title"] or not item["short"] or not item["description"]:
        return JSONResponse({"ok": False, "error": "Заполните все поля"}, status_code=400)

    update_prize(prize_id, item)
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

    if not prize_exists(prize_id):
        return JSONResponse({"ok": False, "error": "Приз не найден"}, status_code=404)

    update_prize_weight(prize_id, data.get("weight", 1))
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

    if not prize_exists(prize_id):
        return JSONResponse({"ok": False, "error": "Приз не найден"}, status_code=404)

    toggle_prize(prize_id)
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

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id
                FROM spin_history
                WHERE prize_id = %s
                LIMIT 1
            """, (prize_id,))
            linked = cur.fetchone()

    if linked:
        return JSONResponse(
            {"ok": False, "error": "Нельзя удалить приз, который уже есть в истории"},
            status_code=400
        )

    if not prize_exists(prize_id):
        return JSONResponse({"ok": False, "error": "Приз не найден"}, status_code=404)

    delete_prize(prize_id)
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

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT *
                FROM spin_history
                WHERE code = %s
                LIMIT 1
            """, (code,))
            item = cur.fetchone()

    if not item:
        return {"ok": False, "error": "Код не найден"}

    return {
        "ok": True,
        "code": item.get("code"),
        "redeemed": item.get("redeemed", False),
        "prize_title": item.get("prize_title", "Приз"),
        "prize_weight": item.get("prize_weight"),
        "prize_drop_percent": item.get("prize_drop_percent"),
        "prize_image_url": item.get("prize_image_url", ""),
    }


@app.post("/admin/code/redeem")
async def admin_code_redeem(request: Request):
    data = await request.json()
    init_data = data.get("init_data", "")
    code = str(data.get("code", "")).strip()

    user = get_user_from_init_data(init_data)
    if not is_admin(user):
        return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT *
                FROM spin_history
                WHERE code = %s
                LIMIT 1
            """, (code,))
            item = cur.fetchone()

            if not item:
                return {"ok": False, "error": "Код не найден"}

            if item.get("redeemed"):
                return {
                    "ok": False,
                    "error": "Код уже погашен",
                    "code": item.get("code"),
                    "prize_title": item.get("prize_title", "Приз"),
                }

            cur.execute("""
                UPDATE spin_history
                SET redeemed = TRUE
                WHERE code = %s
            """, (code,))
        conn.commit()

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
