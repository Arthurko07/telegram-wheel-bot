import os
import asyncio
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEB_APP_URL = "https://script.google.com/macros/s/AKfycbykbHhsuM_9z71XoEmu8kNitbBZLUBTOrjd5TexM4UHI2lF6wl_SahWhEM5ThLqiE2xaQ/exec"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(CommandStart())
async def start_handler(message: Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎡 Открыть колесо",
                    web_app=WebAppInfo(url=WEB_APP_URL)
                )
            ]
        ]
    )

    await message.answer(
        "Нажми на кнопку ниже, чтобы открыть колесо бонусов iGadget прямо в Telegram",
        reply_markup=keyboard
    )

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())