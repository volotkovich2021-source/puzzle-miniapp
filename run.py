"""
Запускает и веб-сервер, и телеграм-бота в одном процессе.
На хостинге Render команда запуска: python run.py
"""
import asyncio
import os

import uvicorn

import bot as bot_app
import server


async def serve_web():
    port = int(os.getenv("PORT", "8000"))
    config = uvicorn.Config(server.app, host="0.0.0.0", port=port, log_level="info")
    await uvicorn.Server(config).serve()


async def main():
    tasks = [asyncio.create_task(serve_web())]

    token = os.getenv("BOT_TOKEN", "").strip()
    webapp = os.getenv("WEBAPP_URL", "").strip()
    if token and webapp.startswith("https://"):
        tasks.append(asyncio.create_task(bot_app.main()))
        print("Бот включён.")
    else:
        print("Бот пока выключен: не заданы BOT_TOKEN и WEBAPP_URL.")

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
