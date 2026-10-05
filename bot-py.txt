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


def app_url(room: str = "") -> str:
    """Адрес игры; для комнаты добавляем ?room=КОД."""
    if not room:
        return WEBAPP_URL
    sep = "&" if "?" in WEBAPP_URL else "?"
    return WEBAPP_URL + sep + "room=" + room


def play_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🧩 Открыть пазл",
                web_app=WebAppInfo(url=app_url()),
            )
        ]]
    )


def join_keyboard(code: str) -> InlineKeyboardMarkup:
    """Кнопка, которая открывает игру сразу с кодом комнаты друга."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🧩 Войти в игру",
                web_app=WebAppInfo(url=app_url(code)),
            )
        ]]
    )


def room_code_from(text: str) -> str:
    """Достаём код комнаты из ссылки-приглашения: /start r1234."""
    parts = text.split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""
    if len(payload) == 5 and payload[0] == "r" and payload[1:].isdigit():
        return payload[1:]
    return ""


@dp.message()
async def on_any(message: Message):
    text = (message.text or "").strip()
    if text.startswith("/start"):
        code = room_code_from(text)
        if code:
            await message.answer(
                "Тебя позвали играть вдвоём 🧩\n\n"
                "Нажми кнопку ниже — и попадёшь в игру друга.",
                reply_markup=join_keyboard(code),
            )
            return
    await message.answer(
        "Привет! Собирай пазл из своего фото или сгенерируй картинку по описанию.\n\n"
        "Выбирай режим, сложность и картинку — потом собирай.",
        reply_markup=play_keyboard(),
    )


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
