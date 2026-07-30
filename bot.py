import os
import json
import logging
import random
from typing import Dict, Any

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)

PRIZES = [
    {"id": "bonus_5", "title": "Скидка 5%"},
    {"id": "bonus_10", "title": "Скидка 10%"},
    {"id": "bonus_15", "title": "Скидка 15%"},
    {"id": "gift_case", "title": "Подарок: чехол"},
    {"id": "gift_glass", "title": "Подарок: защитное стекло"},
    {"id": "try_again", "title": "Попробуйте ещё раз"},
]

user_state: Dict[int, Dict[str, Any]] = {}


def get_user_data(user_id: int) -> Dict[str, Any]:
    if user_id not in user_state:
        user_state[user_id] = {
            "spins_used": 0,
            "history": []
        }
    return user_state[user_id]


def spin_wheel_for_user(user_id: int) -> Dict[str, Any]:
    state = get_user_data(user_id)

    if state["spins_used"] >= 1:
        return {
            "ok": False,
            "reason": "limit_reached",
            "message": "Ты уже использовал свою попытку."
        }

    prize = random.choice(PRIZES)
    state["spins_used"] += 1
    state["history"].append(prize)

    return {
        "ok": True,
        "prize": prize,
        "spins_used": state["spins_used"]
    }


@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    text = (
        "Привет! Открывай Mini App через кнопку <b>«Бонусы iGadget»</b> внизу чата.\n\n"
        "Важно:\n"
        "— отдельные клавиатуры бот не показывает;\n"
        "— нужна только нижняя кнопка Bot Menu / Mini App;\n"
        "— результат после вращения вернётся в этот чат."
    )
    await message.answer(text)


@dp.message_handler(commands=["help"])
async def cmd_help(message: types.Message):
    text = (
        "Как пользоваться:\n"
        "1. Нажми кнопку <b>«Бонусы iGadget»</b> внизу.\n"
        "2. В Mini App нажми кнопку запуска.\n"
        "3. Приложение отправит данные обратно в бота.\n"
        "4. Бот пришлёт результат в чат."
    )
    await message.answer(text)


@dp.message_handler(commands=["reset"])
async def cmd_reset(message: types.Message):
    user_id = message.from_user.id
    user_state[user_id] = {
        "spins_used": 0,
        "history": []
    }
    await message.answer("Твоя попытка сброшена. Можно тестировать заново.")


@dp.message_handler(commands=["me"])
async def cmd_me(message: types.Message):
    user = message.from_user
    state = get_user_data(user.id)

    text = (
        f"<b>Твой профиль</b>\n"
        f"ID: <code>{user.id}</code>\n"
        f"Username: @{user.username if user.username else 'нет'}\n"
        f"Имя: {user.first_name}\n"
        f"Использовано попыток: {state['spins_used']}"
    )
    await message.answer(text)


@dp.message_handler(content_types=types.ContentType.WEB_APP_DATA)
async def handle_web_app_data(message: types.Message):
    user = message.from_user

    if not user:
        await message.answer("Ошибка: не удалось определить пользователя.")
        return

    if not message.web_app_data or not message.web_app_data.data:
        await message.answer("Ошибка: Mini App не передал данные.")
        return

    raw_data = message.web_app_data.data
    logging.info("WEB_APP_DATA user_id=%s data=%s", user.id, raw_data)

    try:
        payload = json.loads(raw_data)
    except json.JSONDecodeError:
        await message.answer("Ошибка: пришёл некорректный JSON из Mini App.")
        return

    action = payload.get("action")

    if action == "ping":
        text = (
            f"Связь с Mini App работает.\n\n"
            f"<b>Telegram ID:</b> <code>{user.id}</code>\n"
            f"<b>Username:</b> @{user.username if user.username else 'нет'}\n"
            f"<b>Имя:</b> {user.first_name}"
        )
        await message.answer(text)
        return

    if action == "spin":
        result = spin_wheel_for_user(user.id)

        if not result["ok"]:
            await message.answer(result["message"])
            return

        prize = result["prize"]

        text = (
            f"🎉 <b>Результат вращения</b>\n\n"
            f"<b>Пользователь:</b> {user.first_name}\n"
            f"<b>Telegram ID:</b> <code>{user.id}</code>\n"
            f"<b>Приз:</b> {prize['title']}\n"
            f"<b>Попыток использовано:</b> {result['spins_used']}"
        )
        await message.answer(text)
        return

    if action == "get_profile":
        state = get_user_data(user.id)
        text = (
            f"<b>Профиль из Mini App</b>\n"
            f"ID: <code>{user.id}</code>\n"
            f"Username: @{user.username if user.username else 'нет'}\n"
            f"Имя: {user.first_name}\n"
            f"Использовано попыток: {state['spins_used']}"
        )
        await message.answer(text)
        return

    await message.answer("Mini App отправил неизвестное действие.")


@dp.message_handler(content_types=types.ContentTypes.ANY)
async def fallback_handler(message: types.Message):
    if message.text:
        text = message.text.strip().lower()

        if text in ["меню", "бонусы", "колесо", "бонусы igadget", "открыть"]:
            await message.answer(
                "Открывай Mini App через кнопку <b>«Бонусы iGadget»</b> внизу чата."
            )
            return

    await message.answer(
        "Используй кнопку <b>«Бонусы iGadget»</b> внизу чата или команду /help."
    )


if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
