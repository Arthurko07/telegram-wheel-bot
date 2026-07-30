import os
import uuid
import logging
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
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

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
    BotCommand,
)
from aiogram.utils.web_app import safe_parse_webapp_init_data

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./wheel.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
CORS_ORIGINS_RAW = os.getenv("CORS_ORIGINS", "*")
WEB_APP_URL = os.getenv("WEB_APP_URL", "https://telegram-wheel-bot-production-764c.up.railway.app")
APP_VERSION = "11.0.0-full-bundle"

UPLOAD_DIR = "uploads"
STATIC_DIR = "static"
INDEX_FILE = os.path.join(STATIC_DIR, "index.html")

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
os.makedirs(STATIC_DIR, exist_ok=True)

bot: Optional[Bot] = None
dp: Optional[Dispatcher] = None
polling_task: Optional[asyncio.Task] = None


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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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
    import secrets
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
    import secrets
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


def parse_admin_ids() -> set[int]:
    return {int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()}


def extract_init_data(payload: dict) -> str:
    raw = payload.get("init_data") or payload.get("initData") or ""
    raw = str(raw).strip()
    if not raw:
        raise HTTPException(status_code=401, detail="init_data is required")
    return raw


def get_or_create_user_from_init_data(init_data: str, db: Session) -> User:
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN not configured")

    try:
        parsed = safe_parse_webapp_init_data(token=BOT_TOKEN, init_data=init_data)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=f"Telegram auth failed: {str(e)}")

    if not parsed.user:
        raise HTTPException(status_code=401, detail="Telegram user not found in init_data")

    tg_user = parsed.user
    telegram_id = int(tg_user.id)
    is_admin = telegram_id in parse_admin_ids()

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        user = User(
            telegram_id=telegram_id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            last_name=tg_user.last_name,
            is_admin=is_admin,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        user.username = tg_user.username
        user.first_name = tg_user.first_name
        user.last_name = tg_user.last_name
        user.is_admin = is_admin
        db.commit()
        db.refresh(user)

    return user


def require_user(payload: dict, db: Session) -> User:
    init_data = extract_init_data(payload)
    return get_or_create_user_from_init_data(init_data, db)


def require_admin(payload: dict, db: Session) -> User:
    user = require_user(payload, db)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def build_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎡 Открыть колесо", web_app=WebAppInfo(url=WEB_APP_URL))],
            [KeyboardButton(text="ℹ️ Как это работает"), KeyboardButton(text="🔄 Открыть заново")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


async def cmd_start(message: Message):
    await message.answer(
        "Привет! Нажми кнопку ниже, чтобы открыть Mini App.",
        reply_markup=build_main_keyboard(),
    )


async def cmd_help(message: Message):
    await message.answer(
        "1. Открой Mini App\n2. Проверь initData\n3. Крути колесо",
        reply_markup=build_main_keyboard(),
    )


async def handle_text(message: Message):
    text = (message.text or "").strip()
    if text == "ℹ️ Как это работает":
        await cmd_help(message)
        return
    if text in {"🔄 Открыть заново", "🎡 Открыть колесо"}:
        await message.answer("Нажми кнопку Mini App ниже.", reply_markup=build_main_keyboard())
        return
    await message.answer("Используй /start.", reply_markup=build_main_keyboard())


async def handle_web_app_data(message: Message):
    await message.answer("Данные Mini App получены.", reply_markup=build_main_keyboard())


def register_aiogram_handlers(dispatcher: Dispatcher):
    dispatcher.message.register(cmd_start, CommandStart())
    dispatcher.message.register(cmd_help, Command("help"))
    dispatcher.message.register(handle_web_app_data, F.web_app_data)
    dispatcher.message.register(handle_text, F.text)


async def start_aiogram_bot():
    global bot, dp, polling_task
    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN is empty, aiogram bot will not start")
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    register_aiogram_handlers(dp)

    await bot.delete_webhook(drop_pending_updates=False)
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="help", description="Помощь"),
    ])

    me = await bot.get_me()
    logger.info("Aiogram bot started as @%s (%s)", me.username, me.id)

    polling_task = asyncio.create_task(
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    )


async def stop_aiogram_bot():
    global bot, dp, polling_task
    if polling_task:
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("Polling stop failed: %s", e)
    if bot:
        await bot.session.close()
        logger.info("Aiogram bot stopped")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting unified app %s", APP_VERSION)
    await start_aiogram_bot()
    yield
    await stop_aiogram_bot()
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

if os.path.isdir(UPLOAD_DIR):
    app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def mini_app_root():
    if os.path.exists(INDEX_FILE):
        return FileResponse(INDEX_FILE)
    return {"ok": True, "message": "Put frontend into static/index.html", "version": APP_VERSION}


@app.get("/health")
async def health():
    return {"ok": True, "service": "telegram-wheel-bot-api", "version": APP_VERSION}


@app.get("/debug/env")
async def debug_env():
    return {
        "ok": True,
        "version": APP_VERSION,
        "bot_token_configured": bool(BOT_TOKEN),
        "web_app_url": WEB_APP_URL,
        "admin_ids": list(parse_admin_ids()),
        "has_index_html": os.path.exists(INDEX_FILE),
    }


@app.post("/debug/check-init-data")
async def debug_check_init_data(payload: dict):
    init_data = extract_init_data(payload)
    if not BOT_TOKEN:
        return {"ok": False, "detail": "BOT_TOKEN not configured"}

    try:
        parsed = safe_parse_webapp_init_data(token=BOT_TOKEN, init_data=init_data)
        user = parsed.user
        return {
            "ok": True,
            "auth_date": str(parsed.auth_date) if parsed.auth_date else None,
            "chat_type": parsed.chat_type,
            "chat_instance": parsed.chat_instance,
            "query_id": parsed.query_id,
            "start_param": parsed.start_param,
            "user": {
                "id": user.id if user else None,
                "username": user.username if user else None,
                "first_name": user.first_name if user else None,
                "last_name": user.last_name if user else None,
            } if user else None,
        }
    except ValueError as e:
        return {"ok": False, "detail": str(e)}


@app.get("/prizes")
async def get_prizes(db: Session = Depends(get_db)):
    items = db.query(Prize).filter(Prize.active == True).order_by(Prize.id.asc()).all()
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

    available_prizes = db.query(Prize).filter(Prize.active == True, Prize.weight > 0).all()
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
    items = db.query(SpinResult).filter(SpinResult.user_id == user.id).order_by(SpinResult.id.desc()).all()
    return {"ok": True, "items": [spin_to_dict(item) for item in items]}


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
    return {"ok": True, "filename": filename, "image_url": image_url, "prize_image_url": image_url}


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
    return {"ok": True, "item": serialize_prize(prize), "items": [serialize_prize(item) for item in items]}


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
    return {"ok": True, "item": serialize_prize(prize), "items": [serialize_prize(item) for item in items]}


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
