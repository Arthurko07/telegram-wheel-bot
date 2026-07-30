import os
import hmac
import json
import uuid
import time
import hashlib
import secrets
from pathlib import Path
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

# =========================================================
# CONFIG
# =========================================================

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./wheel.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
CORS_ORIGINS_RAW = os.getenv("CORS_ORIGINS", "*")
DEBUG_AUTH = os.getenv("DEBUG_AUTH", "true").lower() == "true"

UPLOAD_DIR = "uploads"
MAX_FILE_SIZE_MB = 10
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/jpg"}

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

app = FastAPI(title="Telegram Wheel Bot API", version="7.2.0-debug-auth")

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
# SQLITE LIGHT MIGRATION
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
# DEPENDENCIES
# =========================================================

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# =========================================================
# HELPERS
# =========================================================

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

# =========================================================
# TELEGRAM AUTH
# =========================================================

def parse_init_data(init_data: str) -> dict:
    pairs = [chunk.split("=", 1) for chunk in init_data.split("&") if "=" in chunk]
    return {k: v for k, v in pairs}

def build_data_check_string(data: dict) -> str:
    return "\n".join(f"{k}={v}" for k, v in sorted(data.items()))

def verify_telegram_init_data(init_data: str) -> dict:
    if not init_data:
      File "/app/.venv/lib/python3.13/site-packages/starlette/routing.py", line 75, in app
        response = await f(request)
                   ^^^^^^^^^^^^^^^^
      File "/app/.venv/lib/python3.13/site-packages/fastapi/routing.py", line 302, in app
        raw_response = await run_endpoint_function(
                       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
        ...<3 lines>...
        )
        ^
      File "/app/.venv/lib/python3.13/site-packages/fastapi/routing.py", line 213, in run_endpoint_function
        return await dependant.call(**values)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
      File "/app/bot.py", line 220
        File "/app/.venv/lib/python3.13/site-packages/starlette/routing.py", line 75, in app
                                                                                      ^
    IndentationError: unindent does not match any outer indentation level
