"""
Сервер мини-приложения «Пазл».

Отдаёт страницу игры и проксирует генерацию картинок,
чтобы браузер не упирался в CORS и в водяные знаки.

Запуск:
    uvicorn server:app --host 0.0.0.0 --port 8000
"""
import os
import random
import urllib.parse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
POLLINATIONS = "https://image.pollinations.ai/prompt/"
TRANSLATE = "https://text.pollinations.ai/"

app = FastAPI(title="Puzzle Mini App")


class GenerateRequest(BaseModel):
    prompt: str


@app.get("/")
async def index():
    """Сама страница с пазлом."""
    return FileResponse(os.path.join(HERE, "puzzle-app.html"))


@app.get("/health")
async def health():
    return {"ok": True}


async def to_english(prompt: str) -> str:
    """Переводит промпт на английский: генератор картинок лучше понимает его."""
    ask = "Translate to English. Answer with the translation only, no extra words: " + prompt
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            r = await client.get(TRANSLATE + urllib.parse.quote(ask))
        text = (r.text or "").strip().strip('"').strip()
        if text and len(text) <= 300:
            return text
    except httpx.HTTPError:
        pass
    return prompt


@app.post("/api/generate")
async def generate(req: GenerateRequest):
    """Генерирует картинку по тексту и отдаёт её картинкой же (без CORS-проблем)."""
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Пустой запрос")
    prompt = prompt[:300]

    # переводим на английский — модель рисования понимает его лучше
    english = await to_english(prompt)

    # seed добавляет разнообразие: один и тот же текст даёт новые картинки
    seed = random.randint(1, 2_000_000_000)
    url = (
        POLLINATIONS
        + urllib.parse.quote(english)
        + f"?width=768&height=768&nologo=true&enhance=true&seed={seed}"
    )

    try:
        async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as client:
            r = await client.get(url)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Генератор недоступен, попробуй позже")

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Генератор ответил {r.status_code}")

    media = r.headers.get("content-type", "image/jpeg")
    return Response(content=r.content, media_type=media)