"""Read-only JSON-API fuer das Scriptable-Widget.

Laeuft im gleichen Image wie die Bots, liest die SQLite-DB nur lesend.
Auth: Bearer-Token aus API_TOKEN (in .env). Ohne Token -> 401.

    GET /api/health                     (kein Token)
    GET /api/summary                    Kennzahlen aller Bots inkl. Kurve ("spark") und
                                        Aenderungen ("changes": start/month/week/day in %)
    GET /api/equity?bot=wsb&limit=500   Equity-Kurve eines Bots
"""
from __future__ import annotations

import base64
import binascii
import hmac
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

DB_PATH = Path(os.getenv("DATA_DIR", "/app/data")) / "bots.db"
API_TOKEN = os.getenv("API_TOKEN", "").strip()

LABELS = {
    "wsb": "WallStreetBets",
    "truth": "Truth Social",
    "armstrong": "Armstrong Economics",
}
BOT_ORDER = ["wsb", "truth", "armstrong"]

app = FastAPI(title="tradebot-compare API", docs_url=None, redoc_url=None, openapi_url=None)


def _authorized(authorization: str | None, token: str | None) -> bool:
    """Akzeptiert Bearer-Token, ?token= und HTTP-Basic.

    Bei Basic zaehlt das Passwort (der Benutzername wird von nginx geprueft),
    damit Webseite und Widget dasselbe Passwort verwenden koennen.
    """
    if not API_TOKEN:
        # Ohne konfiguriertes Token bleibt die API zu, statt offen zu sein.
        return False

    provided = ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        value = value.strip()
        if scheme.lower() == "bearer":
            provided = value
        elif scheme.lower() == "basic":
            try:
                decoded = base64.b64decode(value).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                return False
            provided = decoded.partition(":")[2]
    elif token:
        provided = token.strip()

    return hmac.compare_digest(provided.encode(), API_TOKEN.encode())


def _require_auth(authorization: str | None, token: str | None) -> None:
    if not API_TOKEN:
        raise HTTPException(status_code=503, detail="API_TOKEN ist nicht gesetzt")
    if not _authorized(authorization, token):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise HTTPException(status_code=503, detail="Noch keine Datenbank vorhanden")
    # mode=ro: die API kann die Bot-DB niemals veraendern.
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)


def _rows(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with _connect() as con:
        con.row_factory = sqlite3.Row
        return list(con.execute(sql, params))


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _max_drawdown(values: list[float]) -> float:
    peak = values[0] if values else 0.0
    worst = 0.0
    for v in values:
        peak = max(peak, v)
        if peak:
            worst = min(worst, v / peak - 1.0)
    return worst * 100.0


def _rows_safe(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    """Wie _rows, gibt aber [] zurueck, wenn die DB fehlt oder leer ist."""
    if not DB_PATH.exists():
        return []
    try:
        with _connect() as con:
            con.row_factory = sqlite3.Row
            return list(con.execute(sql, params))
    except Exception:  # noqa: BLE001
        return []


def _bot_summary(bot: str) -> dict[str, Any]:
    snaps = _rows_safe(
        "SELECT ts, equity, cash, positions_value, n_positions "
        "FROM snapshots WHERE bot=? ORDER BY ts",
        (bot,),
    )
    perf = _performance(bot)
    if not snaps and not perf:
        return {
            "bot": bot,
            "label": LABELS.get(bot, bot),
            "has_data": False,
        }

    if snaps:
        equity = [float(r["equity"]) for r in snaps if r["equity"] is not None]
        first, last = snaps[0], snaps[-1]
        start = float(first["equity"])
        end = float(last["equity"])
        ret = (end / start - 1.0) * 100.0 if start else 0.0
        last_dt = _iso(last["ts"])
    else:
        equity, first, last, ret, end, last_dt = [], None, None, 0.0, 0.0, None

    trade_rows = _rows_safe("SELECT COUNT(*) AS n FROM trades WHERE bot=?", (bot,))
    trade_count = int(trade_rows[0]["n"]) if trade_rows else 0
    now = datetime.now(timezone.utc)
    if perf:  # Alpaca ist die Quelle der Wahrheit
        last_dt = now
    age_minutes = int((now - last_dt).total_seconds() // 60) if last_dt else None

    out = {
        "bot": bot,
        "label": LABELS.get(bot, bot),
        "has_data": True,
        "equity": round(end, 2),
        "return_pct": round(ret, 2),
        "max_drawdown_pct": round(_max_drawdown(equity), 2) if equity else 0.0,
        "trades": trade_count,
        "open_positions": int(last["n_positions"] or 0) if last else 0,
        "cash": round(float(last["cash"] or 0), 2) if last else 0.0,
        "since": (first["ts"] or "")[:10] if first else "",
        "last_ts": last["ts"] if last else None,
        "age_minutes": age_minutes,
        "stale": bool(age_minutes is not None and age_minutes > 180),
        "source": "db",
    }
    if perf:
        out.update(perf)
        out["source"] = "alpaca"
    return out


# ---------- Performance direkt von Alpaca (Kurve + Aenderungen) ----------
_PERF_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_PERF_TTL = 300  # Sekunden – schont die Alpaca-API, Widget fragt eh nur alle paar Minuten
SPARK_POINTS = 90


def _experiment_start(earliest_order: datetime | None = None) -> datetime:
    env = os.getenv("EXPERIMENT_START", "").strip()
    if env:
        dt = _iso(env)
        if dt:
            return dt
    candidates: list[datetime] = []
    for sql in ("SELECT MIN(ts) AS t FROM snapshots", "SELECT MIN(ts) AS t FROM trades"):
        rows = _rows_safe(sql)
        t = _iso(rows[0]["t"]) if rows else None
        if t:
            candidates.append(t.replace(hour=0, minute=0, second=0, microsecond=0))
    if earliest_order:
        candidates.append(earliest_order.replace(hour=0, minute=0, second=0, microsecond=0))
    if candidates:
        return min(candidates)
    return datetime.now(timezone.utc) - timedelta(days=30)


def _ny_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")
    except Exception:  # noqa: BLE001  (z.B. Windows ohne tzdata)
        return timezone(timedelta(hours=-5))


def _changes(points: list[tuple[datetime, float]], now_value: float) -> dict[str, float | None]:
    """Aenderung in % seit Anfang / 30 Tagen / 7 Tagen / letztem Handelstag."""
    now = datetime.now(timezone.utc)

    def pct(ref: float | None) -> float | None:
        return round((now_value / ref - 1.0) * 100.0, 2) if ref else None

    def value_at(cut: datetime) -> float:
        prev = [v for t, v in points if t <= cut]
        return prev[-1] if prev else points[0][1]

    # Tag = letzte (bzw. laufende) Sitzung gegen den Schluss der Sitzung davor
    ny = _ny_tz()
    last_session = points[-1][0].astimezone(ny).date()
    before = [v for t, v in points if t.astimezone(ny).date() < last_session]
    return {
        "start": pct(points[0][1]),
        "month": pct(value_at(now - timedelta(days=30))),
        "week": pct(value_at(now - timedelta(days=7))),
        "day": pct(before[-1] if before else points[0][1]),
    }


def _performance(bot: str) -> dict[str, Any] | None:
    cached = _PERF_CACHE.get(bot)
    if cached and time.time() - cached[0] < _PERF_TTL:
        return cached[1]
    result: dict[str, Any] | None = None
    try:
        from tradebots.broker import Broker
        from tradebots.config import bot_config

        broker = Broker(bot_config(bot))
        orders = broker.filled_orders()
        order_times = [o.filled_at or o.submitted_at for o in orders if (o.filled_at or o.submitted_at)]
        earliest_order = min(order_times) if order_times else None
        points = broker.equity_history(_experiment_start(earliest_order))
        acct = broker.account()
        n_pos = len(broker.positions())
        now_value = float(acct.equity)
        if points:
            first_t, first_v = points[0]
            series = points + [(datetime.now(timezone.utc), now_value)]
            step = max(1, len(series) // SPARK_POINTS)
            sampled = series[::step]
            if sampled[-1] is not series[-1]:
                sampled.append(series[-1])
            result = {
                "equity": round(now_value, 2),
                "cash": round(float(acct.cash), 2),
                "open_positions": n_pos,
                "trades": len(orders),
                "return_pct": round((now_value / first_v - 1.0) * 100.0, 2),
                "max_drawdown_pct": round(_max_drawdown([v for _, v in series]), 2),
                "since": first_t.date().isoformat(),
                "changes": _changes(points, now_value),
                "spark": [[int(t.timestamp()), round((v / first_v - 1.0) * 100.0, 3)] for t, v in sampled],
            }
    except Exception:  # noqa: BLE001  -> Fallback auf lokale DB
        result = None
    _PERF_CACHE[bot] = (time.time(), result)
    return result


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "db": DB_PATH.exists(),
        "auth_configured": bool(API_TOKEN),
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/summary")
def summary(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> JSONResponse:
    _require_auth(authorization, token)
    bots = [_bot_summary(bot) for bot in BOT_ORDER]
    active = [b for b in bots if b["has_data"]]
    return JSONResponse(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "bots": bots,
            "leader": max(active, key=lambda b: b["return_pct"])["bot"] if active else None,
        }
    )


@app.get("/api/equity")
def equity(
    bot: str = Query(default="wsb"),
    limit: int = Query(default=500, ge=1, le=5000),
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> JSONResponse:
    _require_auth(authorization, token)
    if bot not in LABELS:
        raise HTTPException(status_code=404, detail=f"Unbekannter Bot: {bot}")
    rows = _rows(
        "SELECT ts, equity FROM snapshots WHERE bot=? ORDER BY ts DESC LIMIT ?",
        (bot, limit),
    )
    rows.reverse()
    return JSONResponse(
        {
            "bot": bot,
            "label": LABELS[bot],
            "points": [{"ts": r["ts"], "equity": r["equity"]} for r in rows],
        }
    )
