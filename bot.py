"""
Телеграм-бот для мини-приложения «Пазл».

Запуск (после того как сервер поднят и доступен по HTTPS):
    BOT_TOKEN=123456:ABC... WEBAPP_URL=https://your-domain.example/ python bot.py
"""
import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()

dp = Dispatcher()


def play_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🧩 Открыть пазл",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )
        ]]
    )


@dp.message(CommandStart())
async def on_start(message: Message):
    await message.answer(
        "Привет! Собирай пазл из своего фото или сгенерируй картинку по описанию.\n\n"
        "Выбирай сложность 3×3, 4×4 или 5×5 и собирай.",
        reply_markup=play_keyboard(),
    )


@dp.message()
async def on_any(message: Message):
    await message.answer("Нажми кнопку ниже, чтобы играть.", reply_markup=play_keyboard())


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN — возьми токен у @BotFather")
    if not WEBAPP_URL.startswith("https://"):
        raise SystemExit("WEBAPP_URL должен быть публичным адресом https://")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    # кнопка меню слева от поля ввода — всегда под рукой
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Пазл", web_app=WebAppInfo(url=WEBAPP_URL))
        )
    except Exception as exc:  # не критично
        print("Кнопку меню поставить не удалось:", exc)

    await bot.delete_webhook(drop_pending_updates=True)
    print("Бот запущен. Жми Ctrl+C, чтобы остановить.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
