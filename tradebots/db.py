"""SQLite-Speicher (WAL-Modus, damit mehrere Bot-Container parallel schreiben koennen)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    bot TEXT NOT NULL, item_id TEXT NOT NULL, created_utc TEXT, author TEXT,
    title TEXT, text TEXT, url TEXT, fetched_at TEXT, analyzed INTEGER DEFAULT 0,
    PRIMARY KEY (bot, item_id)
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, bot TEXT NOT NULL, created_at TEXT NOT NULL,
    ticker TEXT NOT NULL, action TEXT NOT NULL, confidence REAL, horizon_days INTEGER,
    reason TEXT, source_items TEXT, status TEXT DEFAULT 'pending', status_note TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, bot TEXT NOT NULL, ts TEXT NOT NULL, ticker TEXT NOT NULL,
    side TEXT NOT NULL, notional REAL, qty REAL, order_id TEXT, reason TEXT, signal_ids TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    bot TEXT NOT NULL, ts TEXT NOT NULL, equity REAL, cash REAL, positions_value REAL, n_positions INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, bot TEXT, ts TEXT, level TEXT, message TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_bot_status ON signals(bot, status);
CREATE INDEX IF NOT EXISTS idx_snap_bot_ts ON snapshots(bot, ts);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)


def log_event(bot: str, level: str, message: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO events(bot, ts, level, message) VALUES (?,?,?,?)", (bot, iso(), level, message[:2000]))


# ---------- items ----------
def insert_items(bot: str, items: list[dict]) -> list[dict]:
    """Speichert Items, gibt nur die tatsaechlich neuen zurueck."""
    new = []
    with conn() as c:
        for it in items:
            cur = c.execute(
                "INSERT OR IGNORE INTO items(bot,item_id,created_utc,author,title,text,url,fetched_at) VALUES (?,?,?,?,?,?,?,?)",
                (bot, it["item_id"], it.get("created_utc"), it.get("author"), it.get("title"), it.get("text"), it.get("url"), iso()),
            )
            if cur.rowcount:
                new.append(it)
    return new


def mark_analyzed(bot: str, item_ids: list[str], value: int = 1) -> None:
    with conn() as c:
        c.executemany("UPDATE items SET analyzed=? WHERE bot=? AND item_id=?", [(value, bot, i) for i in item_ids])


def recent_items(bot: str, limit: int = 12, exclude_ids: set[str] | None = None) -> list[sqlite3.Row]:
    """Letzte bereits ausgewertete Posts dieses Bots als Gedaechtnis."""
    with conn() as c:
        rows = c.execute(
            "SELECT item_id, created_utc, author, title, text FROM items WHERE bot=? AND analyzed=1 ORDER BY created_utc DESC LIMIT ?",
            (bot, limit + len(exclude_ids or ())),
        ).fetchall()
    if exclude_ids:
        rows = [r for r in rows if r["item_id"] not in exclude_ids]
    return rows[:limit]


# ---------- signals ----------
def insert_signal(bot: str, s: dict, source_items: list[str]) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO signals(bot,created_at,ticker,action,confidence,horizon_days,reason,source_items,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (bot, iso(), s["ticker"], s["action"], s["confidence"], s.get("horizon_days"), s.get("reason", ""), json.dumps(source_items), iso()),
        )
        return cur.lastrowid


def set_signal_status(ids: list[int], status: str, note: str = "") -> None:
    with conn() as c:
        c.executemany("UPDATE signals SET status=?, status_note=?, updated_at=? WHERE id=?", [(status, note, iso(), i) for i in ids])


def expire_signals(bot: str, ttl_hours: int) -> None:
    cutoff = iso(now() - timedelta(hours=ttl_hours))
    with conn() as c:
        c.execute(
            "UPDATE signals SET status='expired', status_note='TTL abgelaufen', updated_at=? WHERE bot=? AND status='pending' AND created_at < ?",
            (iso(), bot, cutoff),
        )


def pending_signals(bot: str) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM signals WHERE bot=? AND status='pending' ORDER BY created_at", (bot,)).fetchall()


def recent_signals(bot: str, limit: int = 12) -> list[sqlite3.Row]:
    """Letzte Signale dieses Bots (unabhaengig vom Status) als Historie."""
    with conn() as c:
        return c.execute(
            "SELECT created_at, ticker, action, confidence, status, reason FROM signals WHERE bot=? ORDER BY id DESC LIMIT ?",
            (bot, limit),
        ).fetchall()


# ---------- trades / snapshots ----------
def insert_trade(bot: str, ticker: str, side: str, notional: float | None, qty: float | None, order_id: str, reason: str, signal_ids=None) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO trades(bot,ts,ticker,side,notional,qty,order_id,reason,signal_ids) VALUES (?,?,?,?,?,?,?,?,?)",
            (bot, iso(), ticker, side, notional, qty, order_id, reason, json.dumps(signal_ids or [])),
        )


def last_buy_ts(bot: str, ticker: str) -> datetime | None:
    with conn() as c:
        r = c.execute("SELECT ts FROM trades WHERE bot=? AND ticker=? AND side='buy' ORDER BY ts DESC LIMIT 1", (bot, ticker)).fetchone()
    return datetime.fromisoformat(r["ts"]) if r else None


def last_sell_ts(bot: str, ticker: str) -> datetime | None:
    with conn() as c:
        r = c.execute("SELECT ts FROM trades WHERE bot=? AND ticker=? AND side='sell' ORDER BY ts DESC LIMIT 1", (bot, ticker)).fetchone()
    return datetime.fromisoformat(r["ts"]) if r else None


def insert_snapshot(bot: str, equity: float, cash: float, positions_value: float, n_positions: int) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO snapshots(bot,ts,equity,cash,positions_value,n_positions) VALUES (?,?,?,?,?,?)",
            (bot, iso(), equity, cash, positions_value, n_positions),
        )
