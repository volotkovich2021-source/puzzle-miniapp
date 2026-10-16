# -*- coding: utf-8 -*-
"""Работа с базой для игры «Пазл». Подключается, если задан DATABASE_URL.

Модуль ничего не знает про FastAPI и бота: на вход идёт пул соединений,
на выходе — простые словари. Если DATABASE_URL не задан, игра работает как
раньше: комнаты в памяти, без прогресса и рейтинга.
"""
from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from datetime import date as _date, datetime as _dt
from decimal import Decimal
from typing import Any, Optional

import asyncpg

DSN = os.environ.get("DATABASE_URL", "").strip()


def _as_day(value: Any) -> Optional[_date]:
    """'2026-10-07' и date одинаково годятся в аргумент запроса."""
    if value is None or isinstance(value, _date):
        return value
    return _dt.strptime(str(value)[:10], "%Y-%m-%d").date()

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
  tg_id      BIGINT PRIMARY KEY,
  name       TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
  id         BIGSERIAL PRIMARY KEY,
  tg_id      BIGINT NOT NULL REFERENCES players(tg_id) ON DELETE CASCADE,
  picture    TEXT NOT NULL,
  mode       TEXT NOT NULL,
  total      INTEGER NOT NULL,
  seconds    NUMERIC(8,2) NOT NULL,
  room_code  TEXT,
  counted    BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS runs_tg   ON runs (tg_id, created_at DESC);
CREATE INDEX IF NOT EXISTS runs_pic  ON runs (picture, seconds);
CREATE INDEX IF NOT EXISTS runs_when ON runs (created_at DESC);

CREATE TABLE IF NOT EXISTS progress (
  tg_id      BIGINT NOT NULL REFERENCES players(tg_id) ON DELETE CASCADE,
  picture    TEXT   NOT NULL,
  best_pct   SMALLINT NOT NULL DEFAULT 0,
  done       BOOLEAN  NOT NULL DEFAULT FALSE,
  first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tg_id, picture)
);
CREATE INDEX IF NOT EXISTS progress_gal ON progress (tg_id, done, updated_at DESC);

CREATE TABLE IF NOT EXISTS daily (
  day     DATE PRIMARY KEY,
  picture TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_results (
  day        DATE   NOT NULL,
  tg_id      BIGINT NOT NULL REFERENCES players(tg_id) ON DELETE CASCADE,
  name       TEXT,
  seconds    NUMERIC(8,2) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, tg_id)
);
"""

MIN_SECONDS = 3.0          # быстрее этого в зачёт не идёт — так делают скрипты


def enabled() -> bool:
    """Есть ли база. Без неё все функции молча ничего не делают."""
    return bool(DSN)


def _dsn_for_asyncpg(raw: str):
    """Neon отдаёт строку вида ...?sslmode=require&channel_binding=require.
    asyncpg таких параметров не понимает и падает — убираем их сами,
    а шифрование включаем аргументом ssl."""
    try:
        parts = urlsplit(raw)
        if not parts.scheme or not parts.netloc:
            return raw, None
        sslmode = ""
        keep = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key == "sslmode":
                sslmode = value.lower()
            elif key == "channel_binding":
                continue
            else:
                keep.append((key, value))
        clean = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(keep), parts.fragment)
        )
        host = (parts.hostname or "").lower()
        local = (
            not host
            or host in ("localhost", "127.0.0.1", "::1")
            or host.startswith("/")
            or any(key == "host" for key, _ in keep)   # сокет: /var/run/postgresql
        )
        if sslmode in ("require", "verify-ca", "verify-full", "prefer"):
            return clean, "require"
        if not sslmode and not local:
            return clean, "require"      # облачная база — только по TLS
        return clean, None
    except Exception:
        return raw, None


async def open_pool() -> Optional[asyncpg.Pool]:
    if not enabled():
        return None
    dsn, ssl = _dsn_for_asyncpg(DSN)
    pool = await asyncpg.create_pool(
        dsn, ssl=ssl, min_size=1, max_size=4, command_timeout=10,
        statement_cache_size=0,   # чтобы работало и через пулер Neon (pgbouncer)
    )
    async with pool.acquire() as con:
        await con.execute(SCHEMA)
    return pool


# ---------- запись ----------

async def touch_player(pool: asyncpg.Pool, tg_id: int, name: str = "") -> None:
    await pool.execute(
        """INSERT INTO players (tg_id, name) VALUES ($1, $2)
           ON CONFLICT (tg_id) DO UPDATE SET name = COALESCE(NULLIF(EXCLUDED.name, ''), players.name)""",
        int(tg_id), (name or "")[:64],
    )


async def save_run(pool: asyncpg.Pool, tg_id: int, picture: str, mode: str,
                   total: int, seconds: float, room_code: Optional[str] = None,
                   counted: bool = True) -> None:
    await touch_player(pool, tg_id)
    await pool.execute(
        """INSERT INTO runs (tg_id, picture, mode, total, seconds, room_code, counted)
           VALUES ($1, $2, $3, $4, $5, $6, $7)""",
        int(tg_id), picture, mode, int(total), float(seconds), room_code, bool(counted),
    )


async def save_progress(pool: asyncpg.Pool, tg_id: int, picture: str,
                        best_pct: int, done: bool) -> None:
    await touch_player(pool, tg_id)
    await pool.execute(
        """INSERT INTO progress (tg_id, picture, best_pct, done)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (tg_id, picture) DO UPDATE
             SET best_pct   = GREATEST(progress.best_pct, EXCLUDED.best_pct),
                 done       = progress.done OR EXCLUDED.done,
                 updated_at = now()""",
        int(tg_id), picture, max(0, min(100, int(best_pct))), bool(done),
    )


# ---------- чтение ----------

def _clean(row: Any) -> dict[str, Any]:
    """Ряд из базы в вид, который спокойно уезжает в JSON:
    NUMERIC приходит как Decimal, время — как datetime, json их не умеет."""
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, Decimal):
            value = float(value)
        elif isinstance(value, (_dt, _date)):
            value = value.isoformat()
        out[key] = value
    if "name" in out and not out["name"]:
        out["name"] = "Игрок"
    return out


async def my_gallery(pool: asyncpg.Pool, tg_id: int) -> list[dict[str, Any]]:
    """Галерея «Мои пазлы»: собрано — последними."""
    rows = await pool.fetch(
        """SELECT picture, best_pct, done, updated_at
             FROM progress WHERE tg_id = $1
             ORDER BY done, updated_at DESC""",
        int(tg_id),
    )
    return [_clean(r) for r in rows]


async def leaderboard(pool: asyncpg.Pool, day: Optional[str] = None,
                      limit: int = 20) -> list[dict[str, Any]]:
    """Рейтинг: за конкретный день (day='2026-10-07') или за всё время."""
    if day:
        rows = await pool.fetch(
            """SELECT tg_id, name, seconds FROM daily_results
                WHERE day = $1::date ORDER BY seconds ASC LIMIT $2""",
            _as_day(day), int(limit),
        )
    else:
        rows = await pool.fetch(
            """SELECT r.tg_id, COALESCE(p.name, '') AS name, MIN(r.seconds) AS seconds
                 FROM runs r LEFT JOIN players p ON p.tg_id = r.tg_id
                WHERE r.counted AND r.seconds >= $2
                GROUP BY r.tg_id, p.name ORDER BY seconds ASC LIMIT $1""",
            int(limit), MIN_SECONDS,
        )
    return [_clean(r) for r in rows]


async def my_place(pool: asyncpg.Pool, tg_id: int,
                   day: Optional[str] = None) -> Optional[int]:
    """Какое место занимает игрок — 1, 2, 3... или None, если не играл."""
    if day:
        row = await pool.fetchrow(
            """SELECT CASE
                 WHEN NOT EXISTS (SELECT 1 FROM daily_results
                        WHERE day = $1::date AND tg_id = $2) THEN NULL
                 ELSE (SELECT COUNT(*) + 1 FROM daily_results
                        WHERE day = $1::date AND seconds < (
                          SELECT seconds FROM daily_results
                           WHERE day = $1::date AND tg_id = $2))
               END AS place""",
            _as_day(day), int(tg_id),
        )
    else:
        row = await pool.fetchrow(
            """SELECT CASE
                 WHEN NOT EXISTS (SELECT 1 FROM runs
                        WHERE tg_id = $1 AND counted AND seconds >= $2) THEN NULL
                 ELSE (SELECT COUNT(*) + 1 FROM (
                        SELECT tg_id, MIN(seconds) AS seconds FROM runs
                         WHERE counted AND seconds >= $2 GROUP BY tg_id) t
                        WHERE t.seconds < (SELECT MIN(seconds) FROM runs
                          WHERE tg_id = $1 AND counted AND seconds >= $2))
               END AS place""",
            int(tg_id), MIN_SECONDS,
        )
    return int(row["place"]) if row and row["place"] is not None else None


async def daily_picture(pool: asyncpg.Pool, day: Optional[str] = None) -> Optional[str]:
    row = await pool.fetchrow(
        "SELECT picture FROM daily WHERE day = COALESCE($1::date, CURRENT_DATE)", _as_day(day))
    return row["picture"] if row else None


async def set_daily(pool: asyncpg.Pool, picture: str, day: Optional[str] = None) -> None:
    await pool.execute(
        """INSERT INTO daily (day, picture) VALUES (COALESCE($2::date, CURRENT_DATE), $1)
           ON CONFLICT (day) DO NOTHING""",
        picture, _as_day(day),
    )


async def submit_daily(pool: asyncpg.Pool, tg_id: int, name: str, seconds: float,
                       day: Optional[str] = None) -> bool:
    """Одна зачётная попытка в день: повтор не создаёт второй ряд,
    засчитывается лучший результат. Слишком быстрое время не считается."""
    if float(seconds) < MIN_SECONDS:
        return False
    await touch_player(pool, tg_id, name)
    await pool.execute(
        """INSERT INTO daily_results (day, tg_id, name, seconds)
           VALUES (COALESCE($4::date, CURRENT_DATE), $1, $2, $3)
           ON CONFLICT (day, tg_id) DO UPDATE
             SET seconds = LEAST(daily_results.seconds, EXCLUDED.seconds),
                 name    = COALESCE(NULLIF(EXCLUDED.name, ''), daily_results.name)""",
        int(tg_id), (name or "")[:64], float(seconds), _as_day(day),
    )
    return True
