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
CREATE TABLE IF NOT EXISTS positions_meta (
    bot TEXT NOT NULL, ticker TEXT NOT NULL,
    opened_at TEXT NOT NULL, peak_plpc REAL DEFAULT 0.0, horizon_days INTEGER,
    PRIMARY KEY (bot, ticker)
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


def unanalyzed_items(bot: str, since_iso: str) -> list[dict]:
    """Alle frischen noch nicht analysierten Items (holt auch fehlgeschlagene LLM-Batches nach)."""
    with conn() as c:
        rows = c.execute(
            "SELECT item_id, created_utc, author, title, text, url FROM items "
            "WHERE bot=? AND analyzed=0 AND created_utc >= ? ORDER BY created_utc ASC",
            (bot, since_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_stale_unanalyzed(bot: str, since_iso: str) -> int:
    """Markiert ueberalterte analyzed=0 Items als -1, damit sie nicht ewig offen bleiben."""
    with conn() as c:
        cur = c.execute(
            "UPDATE items SET analyzed=-1 WHERE bot=? AND analyzed=0 AND created_utc < ?",
            (bot, since_iso),
        )
        return cur.rowcount


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
def insert_trade(bot: str, ticker: str, side: str, notional: float | None, qty: float | None, order_id: str, reason: str, signal_ids=None, ts: datetime | None = None) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO trades(bot,ts,ticker,side,notional,qty,order_id,reason,signal_ids) VALUES (?,?,?,?,?,?,?,?,?)",
            (bot, iso(ts), ticker, side, notional, qty, order_id, reason, json.dumps(signal_ids or [])),
        )


def has_trade(bot: str, order_id: str) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM trades WHERE bot=? AND order_id=? LIMIT 1", (bot, order_id)).fetchone() is not None


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


# ---------- positions_meta (Peak & Horizont fuer Trailing-Stop / Zeit-Exit) ----------
def upsert_position_meta(bot: str, ticker: str, opened_at: datetime, peak_plpc: float, horizon_days: int | None) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO positions_meta(bot,ticker,opened_at,peak_plpc,horizon_days) VALUES (?,?,?,?,?) "
            "ON CONFLICT(bot,ticker) DO UPDATE SET peak_plpc=MAX(peak_plpc, excluded.peak_plpc), "
            "horizon_days=COALESCE(excluded.horizon_days, horizon_days)",
            (bot, ticker, iso(opened_at), peak_plpc, horizon_days),
        )


def get_position_meta(bot: str, ticker: str) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute("SELECT * FROM positions_meta WHERE bot=? AND ticker=?", (bot, ticker)).fetchone()


def update_position_peak(bot: str, ticker: str, peak_plpc: float) -> None:
    with conn() as c:
        c.execute(
            "UPDATE positions_meta SET peak_plpc=? WHERE bot=? AND ticker=?",
            (peak_plpc, bot, ticker),
        )


def delete_position_meta(bot: str, ticker: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM positions_meta WHERE bot=? AND ticker=?", (bot, ticker))


def prune_positions_meta(bot: str, open_tickers: set[str]) -> None:
    with conn() as c:
        rows = c.execute("SELECT ticker FROM positions_meta WHERE bot=?", (bot,)).fetchall()
        stale = [(bot, r["ticker"]) for r in rows if r["ticker"] not in open_tickers]
        if stale:
            c.executemany("DELETE FROM positions_meta WHERE bot=? AND ticker=?", stale)
