
"""
Сервер мини-приложения «Пазл».

Отдаёт страницу игры, проксирует генерацию картинок,
чтобы браузер не упирался в CORS и в водяные знаки,
и держит комнаты для игры вдвоём.

Запуск:
    uvicorn server:app --host 0.0.0.0 --port 8000
"""
import contextlib
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

try:
    import db          # база: прогресс, галерея, рейтинг
except Exception as _exc:   # без asyncpg игра просто работает как раньше
    db = None
    print("База недоступна:", _exc)

HERE = os.path.dirname(os.path.abspath(__file__))
POLLINATIONS = "https://image.pollinations.ai/prompt/"
TRANSLATE = "https://text.pollinations.ai/"

DB = None            # пул соединений, если задан DATABASE_URL


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    """Поднимаем базу на старте, закрываем на выходе. Без DATABASE_URL — молчим."""
    global DB
    if db is not None:
        try:
            DB = await db.open_pool()
            print("База подключена" if DB else "DATABASE_URL не задан — работаем без базы")
        except Exception as exc:
            print("База не подключилась:", exc)
            DB = None
    yield
    if DB is not None:
        await DB.close()


app = FastAPI(title="Puzzle Mini App", lifespan=lifespan)

ROOM_TTL = 3600      # сколько комната живёт без игроков, секунд
MAX_PLAYERS = 4      # больше четырёх в одну комнату не пускаем

rooms = {}

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "791680946").strip()


class ReportRequest(BaseModel):
    time: int
    mode: str = "puzzle"
    total: int = 0
    name: str = ""
    tg_id: int = 0            # кто играл — из Telegram
    picture: str = ""         # какую картинку собирал
    pct: int = 100            # насколько дошёл, 0..100
    done: bool = True         # собрал целиком
    thumb: str = ""           # маленькое превью своей картинки (data:image/...)


def _clean_thumb(value: str) -> str:
    """Превью принимаем только картинкой и не больше 200 КБ — иначе пусто."""
    text = (value or "").strip()
    if not text.startswith("data:image/"):
        return ""
    if len(text) > 200000:
        return ""
    return text


class DailyResult(BaseModel):
    tg_id: int = 0
    name: str = ""
    seconds: float = 0


async def notify_admin(text: str) -> None:
    """Шлём владельцу короткое сообщение о собранном пазле."""
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                "https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage",
                json={"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "HTML"},
            )
    except Exception as exc:
        print("Уведомление не ушло:", exc)


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


@app.get("/pazl-{name}.png")
async def builtin_picture(name: str):
    """Красочные картинки: их выбирают во вкладке «Готовые», одна из них — картинка дня."""
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_")
    path = os.path.join(HERE, "pazl-" + safe + ".png")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Нет такой картинки")
    return FileResponse(path, media_type="image/png")


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/api/report")
async def report(req: ReportRequest):
    """Игрок собрал пазл — шлём владельцу уведомление и пишем результат в базу."""
    who = (req.name or "").strip() or "Кто-то"
    what = (str(req.total) + " кусочков") if req.mode == "puzzle" else "пятнашки"
    if req.done:
        await notify_admin("🧩 " + who + " собрал за " + str(req.time) + " сек (" + what + ")")
    if DB is not None and req.tg_id:
        try:
            if req.done:   # незаконченную попытку в рейтинг не пишем
                await db.save_run(DB, req.tg_id, req.picture or "без картинки", req.mode,
                                  req.total, req.time)
            await db.save_progress(DB, req.tg_id, req.picture or "без картинки",
                                   req.pct, req.done, _clean_thumb(req.thumb))
        except Exception as exc:
            print("Результат не записался:", exc)
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


# ---------------- прогресс, галерея, рейтинг, картинка дня ----------------

MSK_SHIFT = 3 * 3600      # сервер живёт по UTC, а день считаем по Москве


def today_msk() -> str:
    """Сегодняшняя дата по Москве — по ней меняется картинка дня."""
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + MSK_SHIFT))


# Картинки, из которых выбирается «картинка дня». Порядок менять можно:
# номер дня сам раздаст их по кругу, вручную назначать ничего не нужно.
DAILY_PICTURES = [
    "pazl-01-zakat.png",
    "pazl-02-gory.png",
    "pazl-03-shary.png",
    "pazl-04-cvety.png",
    "pazl-05-more.png",
    "pazl-06-gorod.png",
    "pazl-07-raduga.png",
    "pazl-08-kosmos.png",
]


def picture_for_day(day: str) -> str:
    """Какая картинка выпадает на дату — одинаково для всех и без повторов-подряд."""
    if not DAILY_PICTURES:
        return ""
    number = time.strptime(day, "%Y-%m-%d").tm_yday          # номер дня в году
    return DAILY_PICTURES[number % len(DAILY_PICTURES)]


@app.get("/api/my")
async def my_gallery(tg_id: int):
    """Галерея «Мои пазлы»: собрано — в конце, у каждой свой процент."""
    if DB is None:
        return {"ok": False, "reason": "no_db", "items": []}
    try:
        items = await db.my_gallery(DB, tg_id)
    except Exception as exc:
        print("Галерея не отдалась:", exc)
        return {"ok": False, "reason": "db_error", "items": []}
    return {"ok": True, "items": items}


@app.get("/api/leaderboard")
async def leaderboard(period: str = "all", tg_id: int = 0):
    """Рейтинг: period=today — за сегодня, period=all — за всё время."""
    if DB is None:
        return {"ok": False, "reason": "no_db", "items": [], "my_place": None}
    day = today_msk() if period == "today" else None
    try:
        rows = await db.leaderboard(DB, day)
        place = await db.my_place(DB, tg_id, day) if tg_id else None
    except Exception as exc:
        print("Рейтинг не отдался:", exc)
        return {"ok": False, "reason": "db_error", "items": [], "my_place": None}
    return {"ok": True, "period": period, "items": rows, "my_place": place}


@app.get("/api/daily")
async def daily():
    """Картинка дня: одна на всех, меняется по московской дате.

    Сервер назначает её сам при первом заходе за сутки — вручную
    ничего выставлять не нужно."""
    if DB is None:
        return {"ok": False, "reason": "no_db", "picture": None}
    day = today_msk()
    try:
        picture = await db.daily_picture(DB, day)
        if not picture:
            picture = picture_for_day(day)
            if picture:
                await db.set_daily(DB, picture, day)
    except Exception as exc:
        print("Картинка дня не отдалась:", exc)
        return {"ok": False, "reason": "db_error", "picture": None}
    return {"ok": True, "day": day, "picture": picture}


@app.post("/api/daily/result")
async def daily_result(req: DailyResult):
    """Зачётная попытка дня. Одна на человека: повтор оставляет лучший результат."""
    if DB is None or not req.tg_id:
        return {"ok": False, "reason": "no_db"}
    day = today_msk()
    try:
        counted = await db.submit_daily(DB, req.tg_id, req.name, req.seconds, day)
        rows = await db.leaderboard(DB, day)
        place = await db.my_place(DB, req.tg_id, day)
    except Exception as exc:
        print("Попытка дня не записалась:", exc)
        return {"ok": False, "reason": "db_error"}
    return {"ok": True, "counted": counted, "my_place": place, "items": rows}
