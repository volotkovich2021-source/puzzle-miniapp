"""
Сервер мини-приложения «Пазл».

Отдаёт страницу игры, проксирует генерацию картинок,
чтобы браузер не упирался в CORS и в водяные знаки,
и держит комнаты для игры вдвоём.

Запуск:
    uvicorn server:app --host 0.0.0.0 --port 8000
"""
import json
import os
import random
import secrets
import time
import urllib.parse

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
POLLINATIONS = "https://image.pollinations.ai/prompt/"
TRANSLATE = "https://text.pollinations.ai/"

app = FastAPI(title="Puzzle Mini App")

ROOM_TTL = 3600      # сколько комната живёт без игроков, секунд
MAX_PLAYERS = 4      # больше четырёх в одну комнату не пускаем

rooms = {}


class GenerateRequest(BaseModel):
    prompt: str


class RoomCreate(BaseModel):
    image: str
    psize: int
    pcols: int
    prows: int
    edges_v: list
    edges_h: list
    order: list


class RoomJoin(BaseModel):
    code: str


class Room:
    """Одна комната: картинка, нарезка и кто где поставил кусочек."""

    def __init__(self, code: str, data: dict):
        self.code = code
        self.image = data.get("image") or ""
        self.psize = int(data.get("psize") or 12)
        self.pcols = int(data.get("pcols") or 4)
        self.prows = int(data.get("prows") or 3)
        self.edges_v = data.get("edges_v") or []
        self.edges_h = data.get("edges_h") or []
        self.order = [int(x) for x in (data.get("order") or [])]
        self.placed = {}          # "индекс кусочка" -> номер слота
        self.clients = {}         # соединение -> его короткий номер
        self.touched = time.time()

    def snapshot(self) -> dict:
        return {
            "psize": self.psize,
            "pcols": self.pcols,
            "prows": self.prows,
            "edges_v": self.edges_v,
            "edges_h": self.edges_h,
            "order": self.order,
            "placed": self.placed,
        }

    async def broadcast(self, msg: dict) -> None:
        text = json.dumps(msg, ensure_ascii=False)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.pop(ws, None)


def make_code() -> str:
    """Код комнаты из 4 цифр, которого ещё нет."""
    for _ in range(500):
        code = "".join(secrets.choice("0123456789") for _ in range(4))
        if code not in rooms:
            return code
    raise HTTPException(status_code=503, detail="Слишком много комнат, попробуй позже")


def sweep_rooms() -> None:
    """Убирает заброшенные комнаты, чтобы память не росла."""
    now = time.time()
    for code in list(rooms):
        room = rooms[code]
        if not room.clients and now - room.touched > ROOM_TTL:
            rooms.pop(code, None)


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


# ------------------------- игра вдвоём: комнаты -------------------------

@app.post("/api/room/create")
async def room_create(req: RoomCreate):
    """Хост создаёт комнату: картинка и нарезка запоминаются на сервере."""
    sweep_rooms()
    if not req.image:
        raise HTTPException(status_code=400, detail="Нет картинки")
    code = make_code()
    rooms[code] = Room(code, req.model_dump())
    return {"code": code}


@app.post("/api/room/join")
async def room_join(req: RoomJoin):
    """Второй игрок входит по коду и получает ту же картинку и раскладку."""
    code = (req.code or "").strip()
    room = rooms.get(code)
    if not room:
        raise HTTPException(status_code=404, detail="Комната не найдена — проверь код")
    room.touched = time.time()
    return {"ok": True, "image": room.image, "state": room.snapshot()}


@app.get("/api/room/{code}/exists")
async def room_exists(code: str):
    return {"exists": code in rooms}


@app.websocket("/ws/room/{code}")
async def room_ws(ws: WebSocket, code: str):
    """Живой канал комнаты: сюда летят ходы обоих игроков."""
    room = rooms.get(code)
    if not room:
        await ws.close(code=4004)
        return
    if len(room.clients) >= MAX_PLAYERS:
        await ws.accept()
        await ws.send_text(json.dumps({"type": "full"}, ensure_ascii=False))
        await ws.close()
        return

    await ws.accept()
    sid = secrets.token_hex(4)
    room.clients[ws] = sid
    room.touched = time.time()

    await ws.send_text(json.dumps({
        "type": "hello",
        "sid": sid,
        "state": room.snapshot(),
        "count": len(room.clients),
    }, ensure_ascii=False))
    await room.broadcast({"type": "count", "count": len(room.clients)})

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            kind = msg.get("type")
            room.touched = time.time()

            if kind == "move":
                try:
                    idx = int(msg.get("idx"))
                    slot = int(msg.get("slot", -1))
                except (TypeError, ValueError):
                    continue
                if slot < 0:
                    room.placed.pop(str(idx), None)
                else:
                    room.placed[str(idx)] = slot
                await room.broadcast({"type": "move", "idx": idx, "slot": slot, "sid": sid})

            elif kind == "reset":
                room.placed = {}
                await room.broadcast({"type": "reset", "sid": sid})

            elif kind == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))

    except WebSocketDisconnect:
        pass
    finally:
        room.clients.pop(ws, None)
        await room.broadcast({"type": "count", "count": len(room.clients)})
