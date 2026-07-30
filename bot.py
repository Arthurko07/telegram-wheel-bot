import os
import hmac
import json
import uuid
import time
import hashlib
import secrets
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Boolean,
    Text,
    DateTime,
    ForeignKey,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

from telegram import (
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./wheel.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
CORS_ORIGINS_RAW = os.getenv("CORS_ORIGINS", "*")
DEBUG_AUTH = os.getenv("DEBUG_AUTH", "true").lower() == "true"
WEB_APP_URL = os.getenv("WEB_APP_URL", "https://telegram-wheel-bot-production-764c.up.railway.app")
APP_VERSION = "8.2.0-unified"

UPLOAD_DIR = "uploads"
MAX_FILE_SIZE_MB = 10
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/jpg"}

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("wheel-app")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

os.makedirs(UPLOAD_DIR, exist_ok=True)

telegram_app: Optional[Application] = None

# =========================================================
# MODELS
# =========================================================

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(Integer, unique=True, nullable=False, index=True)
    username = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    is_admin = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    spins = relationship("SpinResult", back_populates="user")


class Prize(Base):
    __tablename__ = "prizes"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    short = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    weight = Column(Integer, nullable=False, default=1)
    active = Column(Boolean, nullable=False, default=True)
    image_url = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    spins = relationship("SpinResult", back_populates="prize")


class SpinResult(Base):
    __tablename__ = "spin_results"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    prize_id = Column(Integer, ForeignKey("prizes.id"), nullable=False, index=True)
    code = Column(String, unique=True, nullable=False, index=True)
    redeemed = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="spins")
    prize = relationship("Prize", back_populates="spins")


Base.metadata.create_all(bind=engine)

# =========================================================
# DB MIGRATION HELPERS
# =========================================================

def ensure_sqlite_column_exists():
    if not DATABASE_URL.startswith("sqlite"):
        return

    with engine.connect() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(prizes)").fetchall()
        columns = {row[1] for row in rows}
        if "image_url" not in columns:
            conn.exec_driver_sql("ALTER TABLE prizes ADD COLUMN image_url VARCHAR")
            conn.commit()

ensure_sqlite_column_exists()

# =========================================================
# HELPERS
# =========================================================

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def log_auth(message: str):
    if DEBUG_AUTH:
        print(f"[TG_AUTH] {message}", flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def moscow_now() -> datetime:
    return now_utc() + timedelta(hours=3)


def format_dt(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return dt.strftime("%d.%m.%Y %H:%M")


def format_expiry_human(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    now = moscow_now()
    delta = dt - now
    if delta.total_seconds() <= 0:
        return "истёк"
    if delta.days == 0:
        return f"сегодня до {dt.strftime('%H:%M')}"
    if delta.days == 1:
        return f"завтра до {dt.strftime('%H:%M')}"
    return dt.strftime("%d.%m.%Y %H:%M")


def build_absolute_upload_url(request: Request, filename: str) -> str:
    return str(request.base_url).rstrip("/") + f"/uploads/{filename}"


def generate_code() -> str:
    return "IG-" + secrets.token_hex(3).upper()


def serialize_prize(prize: Prize) -> dict:
    return {
        "id": prize.id,
        "title": prize.title,
        "short": prize.short,
        "description": prize.description,
        "weight": prize.weight,
        "active": prize.active,
        "image_url": prize.image_url,
        "prize_image_url": prize.image_url,
    }


def spin_to_dict(spin: SpinResult) -> dict:
    prize = spin.prize
    image_url = prize.image_url if prize else None
    return {
        "id": spin.id,
        "prize_id": prize.id if prize else None,
        "prize_title": prize.title if prize else "Приз",
        "prize_description": prize.description if prize else "",
        "code": spin.code,
        "redeemed": spin.redeemed,
        "created_at": spin.created_at.isoformat() if spin.created_at else None,
        "created_at_text": format_dt(spin.created_at),
        "expires_at": spin.expires_at.isoformat() if spin.expires_at else None,
        "expires_at_text": format_expiry_human(spin.expires_at),
        "image_url": image_url,
        "prize_image_url": image_url,
    }


def get_today_bounds_msk_utc_naive():
    now_msk = moscow_now()
    start_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    end_msk = start_msk + timedelta(days=1)
    start_utc = start_msk - timedelta(hours=3)
    end_utc = end_msk - timedelta(hours=3)
    return start_utc, end_utc


def weighted_pick(prizes: list[Prize]) -> Prize:
    total = sum(max(0, p.weight) for p in prizes)
    if total <= 0:
        raise HTTPException(status_code=400, detail="Нет доступных призов")
    pick = secrets.randbelow(total) + 1
    acc = 0
    for prize in prizes:
        acc += max(0, prize.weight)
        if pick <= acc:
            return prize
    return prizes[-1]


def parse_init_data(init_data: str) -> dict:
    pairs = [chunk.split("=", 1) for chunk in init_data.split("&") if "=" in chunk]
    return {k: v for k, v in pairs}


def build_data_check_string(data: dict) -> str:
    return "\n".join(f"{k}={v}" for k, v in sorted(data.items()))


def verify_telegram_init_data(init_data: str) -> dict:
    if not init_data:
        log_auth("ERROR: empty init_data")
        raise HTTPException(status_code=401, detail="init_data is required")

    if not BOT_TOKEN:
        log_auth("ERROR: BOT_TOKEN not configured")
        raise HTTPException(status_code=500, detail="BOT_TOKEN not configured")

    data = parse_init_data(init_data)
    received_hash = data.pop("hash", None)

    if not received_hash:
        log_auth("ERROR: hash missing in init_data")
        raise HTTPException(status_code=401, detail="Invalid Telegram auth data: hash missing")

    auth_date = data.get("auth_date")
    if auth_date:
        try:
            auth_ts = int(auth_date)
            now_ts = int(time.time())
            age = now_ts - auth_ts
            log_auth(f"auth_date present, age={age}s")
            if age > 86400:
                log_auth(f"ERROR: init_data expired, age={age}s")
                raise HTTPException(status_code=401, detail="Telegram init data expired")
        except ValueError:
            log_auth("ERROR: invalid auth_date format")
            raise HTTPException(status_code=401, detail="Invalid auth_date")

    data_check_string = build_data_check_string(data)
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        log_auth("ERROR: hash mismatch")
        log_auth(f"received_hash={received_hash}")
        log_auth(f"calculated_hash={calculated_hash}")
        log_auth(f"data_keys={sorted(list(data.keys()))}")
        log_auth(f"data_check_string={data_check_string}")
        raise HTTPException(status_code=401, detail="Telegram auth verification failed")

    user_raw = data.get("user")
    if not user_raw:
        log_auth("ERROR: user missing in init_data")
        raise HTTPException(status_code=401, detail="Telegram user not found in init_data")

    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        log_auth(f"ERROR: invalid user payload: {user_raw}")
        raise HTTPException(status_code=401, detail="Invalid Telegram user payload")

    log_auth(f"SUCCESS: telegram_id={user.get('id')}, username={user.get('username')}")
    return user


def get_or_create_user_from_init_data(init_data: str, db: Session) -> User:
    tg_user = verify_telegram_init_data(init_data)
    telegram_id = int(tg_user["id"])

    admin_ids = {int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()}
    is_admin = telegram_id in admin_ids

    log_auth(f"user telegram_id={telegram_id}, is_admin={is_admin}")

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        user = User(
            telegram_id=telegram_id,
            username=tg_user.get("username"),
            first_name=tg_user.get("first_name"),
            last_name=tg_user.get("last_name"),
            is_admin=is_admin,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        user.username = tg_user.get("username")
        user.first_name = tg_user.get("first_name")
        user.last_name = tg_user.get("last_name")
        user.is_admin = is_admin
        db.commit()
        db.refresh(user)

    return user


def require_user(payload: dict, db: Session) -> User:
    init_data = payload.get("init_data", "")
    return get_or_create_user_from_init_data(init_data, db)


def require_admin(payload: dict, db: Session) -> User:
    user = require_user(payload, db)
    if not user.is_admin:
        log_auth(f"ERROR: telegram_id={user.telegram_id} is not admin")
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

# =========================================================
# TELEGRAM BOT
# =========================================================

def build_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="🎡 Открыть колесо",
                    web_app=WebAppInfo(url=WEB_APP_URL),
                )
            ],
            [
                KeyboardButton(text="ℹ️ Как это работает"),
                KeyboardButton(text="🔄 Открыть заново"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


async def tg_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    text = (
        "Привет! Это бот колеса бонусов iGadget.\n\n"
        "Нажми кнопку ниже, чтобы открыть Mini App и прокрутить колесо."
    )

    await update.message.reply_text(
        text,
        reply_markup=build_main_keyboard(),
    )


async def tg_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    text = (
        "Как использовать бота:\n"
        "1. Нажми «🎡 Открыть колесо».\n"
        "2. Mini App откроется внутри Telegram.\n"
        "3. Нажми кнопку старта на колесе и получи приз."
    )
    await update.message.reply_text(text, reply_markup=build_main_keyboard())


async def tg_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    if text == "ℹ️ Как это работает":
        await tg_help(update, context)
        return

    if text == "🔄 Открыть заново":
        await update.message.reply_text(
            "Нажми кнопку ниже, чтобы снова открыть приложение.",
            reply_markup=build_main_keyboard(),
        )
        return

    if text == "🎡 Открыть колесо":
        await update.message.reply_text(
            "Открываю Mini App. Если кнопка не сработала, нажми её ещё раз.",
            reply_markup=build_main_keyboard(),
        )
        return

    await update.message.reply_text(
        "Используй /start или кнопку «🎡 Открыть колесо».",
        reply_markup=build_main_keyboard(),
    )


async def tg_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_message.web_app_data:
        return

    await update.effective_message.reply_text(
        "Данные из Mini App получены.",
        reply_markup=build_main_keyboard(),
    )


async def init_telegram_bot():
    global telegram_app

    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN is empty, telegram bot will not start")
        return

    telegram_app = Application.builder().token(BOT_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", tg_start))
    telegram_app.add_handler(CommandHandler("help", tg_help))
    telegram_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, tg_web_app_data))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_text_router))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    try:
        await telegram_app.bot.set_my_commands([
            ("start", "Запустить бота"),
            ("help", "Помощь"),
        ])
        me = await telegram_app.bot.get_me()
        logger.info("Telegram bot started as @%s (%s)", me.username, me.id)
    except Exception as e:
        logger.exception("Telegram bot post-init failed: %s", e)


async def stop_telegram_bot():
    global telegram_app

    if not telegram_app:
        return

    try:
        if telegram_app.updater:
            await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        logger.info("Telegram bot stopped")
    except Exception as e:
        logger.exception("Telegram bot stop failed: %s", e)

# =========================================================
# FASTAPI LIFESPAN
# =========================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting unified app %s", APP_VERSION)
    await init_telegram_bot()
    yield
    await stop_telegram_bot()
    logger.info("Unified app stopped")

app = FastAPI(title="Telegram Wheel Bot API", version=APP_VERSION, lifespan=lifespan)

origins = ["*"] if CORS_ORIGINS_RAW == "*" else [x.strip() for x in CORS_ORIGINS_RAW.split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# =========================================================
# PUBLIC ENDPOINTS
# =========================================================

@app.get("/")
async def root():
    return {"ok": True, "service": "telegram-wheel-bot-api", "version": APP_VERSION}


@app.get("/debug/env")
async def debug_env():
    admin_ids = [x.strip() for x in ADMIN_IDS_RAW.split(",") if x.strip()]
    return {
        "ok": True,
        "version": APP_VERSION,
        "database_url_prefix": DATABASE_URL.split("://")[0] if "://" in DATABASE_URL else DATABASE_URL,
        "bot_token_configured": bool(BOT_TOKEN),
        "bot_token_prefix": BOT_TOKEN[:10] + "..." if BOT_TOKEN else "",
        "admin_ids_raw": admin_ids,
        "debug_auth": DEBUG_AUTH,
        "web_app_url": WEB_APP_URL,
    }


@app.post("/debug/check-init-data")
async def debug_check_init_data(payload: dict):
    init_data = payload.get("init_data", "")
    try:
        user = verify_telegram_init_data(init_data)
        return {"ok": True, "user": user}
    except HTTPException as e:
        return {
            "ok": False,
            "status_code": e.status_code,
            "detail": e.detail,
        }


@app.get("/prizes")
async def get_prizes(db: Session = Depends(get_db)):
    items = (
        db.query(Prize)
        .filter(Prize.active == True)
        .order_by(Prize.id.asc())
        .all()
    )
    return {"ok": True, "items": [serialize_prize(item) for item in items]}


@app.post("/spin")
async def spin(payload: dict, db: Session = Depends(get_db)):
    user = require_user(payload, db)

    day_start, day_end = get_today_bounds_msk_utc_naive()
    today_spin = (
        db.query(SpinResult)
        .filter(
            SpinResult.user_id == user.id,
            SpinResult.created_at >= day_start,
            SpinResult.created_at < day_end,
        )
        .order_by(SpinResult.id.desc())
        .first()
    )

    if today_spin:
        last_prize = today_spin.prize
        return {
            "ok": False,
            "cooldown": True,
            "error": "Сегодня вы уже использовали попытку.",
            "last_code": today_spin.code,
            "last_prize_title": last_prize.title if last_prize else None,
            "last_prize_description": last_prize.description if last_prize else None,
            "last_expires_at": today_spin.expires_at.isoformat() if today_spin.expires_at else None,
            "last_expires_at_text": format_expiry_human(today_spin.expires_at),
            "last_image_url": last_prize.image_url if last_prize else None,
        }

    available_prizes = (
        db.query(Prize)
        .filter(Prize.active == True, Prize.weight > 0)
        .all()
    )
    if not available_prizes:
        raise HTTPException(status_code=400, detail="Нет активных призов")

    prize = weighted_pick(available_prizes)

    created_at = now_utc()
    expires_at = created_at + timedelta(days=7)

    code = generate_code()
    while db.query(SpinResult).filter(SpinResult.code == code).first():
        code = generate_code()

    result = SpinResult(
        user_id=user.id,
        prize_id=prize.id,
        code=code,
        redeemed=False,
        created_at=created_at,
        expires_at=expires_at,
    )

    db.add(result)
    db.commit()
    db.refresh(result)

    return {
        "ok": True,
        "prize_id": prize.id,
        "prize_title": prize.title,
        "prize_description": prize.description,
        "code": result.code,
        "created_at": result.created_at.isoformat(),
        "created_at_text": format_dt(result.created_at),
        "expires_at": result.expires_at.isoformat() if result.expires_at else None,
        "expires_at_text": format_expiry_human(result.expires_at),
        "image_url": prize.image_url,
        "prize_image_url": prize.image_url,
    }


@app.post("/history")
async def history(payload: dict, db: Session = Depends(get_db)):
    user = require_user(payload, db)
    items = (
        db.query(SpinResult)
        .filter(SpinResult.user_id == user.id)
        .order_by(SpinResult.id.desc())
        .all()
    )
    return {"ok": True, "items": [spin_to_dict(item) for item in items]}

# =========================================================
# ADMIN ENDPOINTS
# =========================================================

@app.post("/admin/me")
async def admin_me(payload: dict, db: Session = Depends(get_db)):
    admin = require_admin(payload, db)
    return {
        "ok": True,
        "telegram_id": admin.telegram_id,
        "first_name": admin.first_name,
        "last_name": admin.last_name,
        "username": admin.username,
        "is_admin": admin.is_admin,
    }


@app.post("/admin/upload-prize-image")
async def admin_upload_prize_image(
    request: Request,
    init_data: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    require_admin({"init_data": init_data}, db)

    if not file.filename:
        raise HTTPException(status_code=400, detail="Файл не выбран")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        raise HTTPException(status_code=400, detail="Допустимы только jpg, jpeg, png, webp")

    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Недопустимый content-type файла")

    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)

    size = 0
    with open(filepath, "wb") as buffer:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_SIZE_MB * 1024 * 1024:
                buffer.close()
                if os.path.exists(filepath):
                    os.remove(filepath)
                raise HTTPException(status_code=400, detail=f"Файл слишком большой, максимум {MAX_FILE_SIZE_MB} MB")
            buffer.write(chunk)

    image_url = build_absolute_upload_url(request, filename)

    return {
        "ok": True,
        "filename": filename,
        "image_url": image_url,
        "prize_image_url": image_url,
    }


@app.post("/admin/prizes")
async def admin_prizes(payload: dict, db: Session = Depends(get_db)):
    require_admin(payload, db)
    items = db.query(Prize).order_by(Prize.id.desc()).all()
    return {"ok": True, "items": [serialize_prize(item) for item in items]}


@app.post("/admin/prize/add")
async def admin_prize_add(payload: dict, db: Session = Depends(get_db)):
    require_admin(payload, db)

    title = str(payload.get("title", "")).strip()
    short = str(payload.get("short", "")).strip()
    description = str(payload.get("description", "")).strip()
    image_url = str(payload.get("image_url", "")).strip() or None

    try:
        weight = int(payload.get("weight", 0))
    except Exception:
        weight = 0

    active = bool(payload.get("active", True))

    if not title or not short or not description or weight <= 0:
        raise HTTPException(status_code=400, detail="Некорректные данные приза")

    prize = Prize(
        title=title,
        short=short,
        description=description,
        weight=weight,
        active=active,
        image_url=image_url,
    )

    db.add(prize)
    db.commit()
    db.refresh(prize)

    items = db.query(Prize).order_by(Prize.id.desc()).all()
    return {
        "ok": True,
        "item": serialize_prize(prize),
        "items": [serialize_prize(item) for item in items],
    }


@app.post("/admin/prize/update")
async def admin_prize_update(payload: dict, db: Session = Depends(get_db)):
    require_admin(payload, db)

    prize_id = payload.get("prize_id")
    prize = db.query(Prize).filter(Prize.id == prize_id).first()
    if not prize:
        raise HTTPException(status_code=404, detail="Приз не найден")

    title = str(payload.get("title", prize.title)).strip()
    short = str(payload.get("short", prize.short)).strip()
    description = str(payload.get("description", prize.description)).strip()

    try:
        weight = int(payload.get("weight", prize.weight))
    except Exception:
        raise HTTPException(status_code=400, detail="Некорректный вес")

    active = bool(payload.get("active", prize.active))
    image_url = payload.get("image_url", prize.image_url)
    image_url = str(image_url).strip() if image_url else None

    if not title or not short or not description or weight <= 0:
        raise HTTPException(status_code=400, detail="Некорректные данные приза")

    prize.title = title
    prize.short = short
    prize.description = description
    prize.weight = weight
    prize.active = active
    prize.image_url = image_url

    db.commit()
    db.refresh(prize)

    items = db.query(Prize).order_by(Prize.id.desc()).all()
    return {
        "ok": True,
        "item": serialize_prize(prize),
        "items": [serialize_prize(item) for item in items],
    }


@app.post("/admin/prize/update-weight")
async def admin_prize_update_weight(payload: dict, db: Session = Depends(get_db)):
    require_admin(payload, db)

    prize_id = payload.get("prize_id")
    prize = db.query(Prize).filter(Prize.id == prize_id).first()
    if not prize:
        raise HTTPException(status_code=404, detail="Приз не найден")

    try:
        weight = int(payload.get("weight", 0))
    except Exception:
        raise HTTPException(status_code=400, detail="Некорректный вес")

    if weight <= 0:
        raise HTTPException(status_code=400, detail="Вес должен быть больше 0")

    prize.weight = weight
    db.commit()

    items = db.query(Prize).order_by(Prize.id.desc()).all()
    return {"ok": True, "items": [serialize_prize(item) for item in items]}


@app.post("/admin/prize/toggle")
async def admin_prize_toggle(payload: dict, db: Session = Depends(get_db)):
    require_admin(payload, db)

    prize_id = payload.get("prize_id")
    prize = db.query(Prize).filter(Prize.id == prize_id).first()
    if not prize:
        raise HTTPException(status_code=404, detail="Приз не найден")

    prize.active = not prize.active
    db.commit()

    items = db.query(Prize).order_by(Prize.id.desc()).all()
    return {"ok": True, "items": [serialize_prize(item) for item in items]}


@app.post("/admin/prize/delete")
async def admin_prize_delete(payload: dict, db: Session = Depends(get_db)):
    require_admin(payload, db)

    prize_id = payload.get("prize_id")
    prize = db.query(Prize).filter(Prize.id == prize_id).first()
    if not prize:
        raise HTTPException(status_code=404, detail="Приз не найден")

    if prize.image_url and "/uploads/" in prize.image_url:
        filename = prize.image_url.split("/uploads/")[-1]
        filepath = os.path.join(UPLOAD_DIR, filename)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass

    db.delete(prize)
    db.commit()

    items = db.query(Prize).order_by(Prize.id.desc()).all()
    return {"ok": True, "items": [serialize_prize(item) for item in items]}


@app.post("/admin/code/check")
async def admin_code_check(payload: dict, db: Session = Depends(get_db)):
    require_admin(payload, db)

    code = str(payload.get("code", "")).strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="Код не указан")

    spin = db.query(SpinResult).filter(SpinResult.code == code).first()
    if not spin:
        raise HTTPException(status_code=404, detail="Код не найден")

    prize = spin.prize

    return {
        "ok": True,
        "code": spin.code,
        "redeemed": spin.redeemed,
        "created_at": spin.created_at.isoformat() if spin.created_at else None,
        "created_at_text": format_dt(spin.created_at),
        "expires_at": spin.expires_at.isoformat() if spin.expires_at else None,
        "expires_at_text": format_expiry_human(spin.expires_at),
        "prize_id": prize.id if prize else None,
        "prize_title": prize.title if prize else None,
        "prize_description": prize.description if prize else None,
        "image_url": prize.image_url if prize else None,
        "prize_image_url": prize.image_url if prize else None,
        "user_id": spin.user.telegram_id if spin.user else None,
    }


@app.post("/admin/code/redeem")
async def admin_code_redeem(payload: dict, db: Session = Depends(get_db)):
    require_admin(payload, db)

    code = str(payload.get("code", "")).strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="Код не указан")

    spin = db.query(SpinResult).filter(SpinResult.code == code).first()
    if not spin:
        raise HTTPException(status_code=404, detail="Код не найден")

    if spin.redeemed:
        raise HTTPException(status_code=400, detail="Код уже погашен")

    spin.redeemed = True
    db.commit()
    db.refresh(spin)

    prize = spin.prize

    return {
        "ok": True,
        "code": spin.code,
        "redeemed": spin.redeemed,
        "prize_id": prize.id if prize else None,
        "prize_title": prize.title if prize else None,
        "prize_description": prize.description if prize else None,
        "image_url": prize.image_url if prize else None,
        "prize_image_url": prize.image_url if prize else None,
    }


@app.post("/admin/seed-default-prizes")
async def admin_seed_default_prizes(payload: dict, db: Session = Depends(get_db)):
    require_admin(payload, db)

    if db.query(Prize).count() > 0:
        return {"ok": True, "message": "Призы уже существуют"}

    defaults = [
        {"title": "Скидка 5%", "short": "5%", "description": "Скидка 5% на покупку", "weight": 40, "active": True, "image_url": None},
        {"title": "Скидка 10%", "short": "10%", "description": "Скидка 10% на покупку", "weight": 25, "active": True, "image_url": None},
        {"title": "Скидка 15%", "short": "15%", "description": "Скидка 15% на аксессуары", "weight": 15, "active": True, "image_url": None},
        {"title": "Бесплатная доставка", "short": "Доставка", "description": "Бесплатная доставка заказа", "weight": 10, "active": True, "image_url": None},
        {"title": "Подарок", "short": "Подарок", "description": "Подарок к покупке", "weight": 6, "active": True, "image_url": None},
        {"title": "Бонус", "short": "Бонус", "description": "Бонус на следующий заказ", "weight": 4, "active": True, "image_url": None},
    ]

    for item in defaults:
        db.add(Prize(**item))
    db.commit()

    return {"ok": True, "message": "Дефолтные призы созданы"}
