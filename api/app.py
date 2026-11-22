"""Read-only JSON-API fuer das Scriptable-Widget.

Laeuft im gleichen Image wie die Bots, liest die SQLite-DB nur lesend.
Auth: Bearer-Token aus API_TOKEN (in .env). Ohne Token -> 401.

    GET /api/health                     (kein Token)
    GET /api/summary                    Kennzahlen aller Bots (fuer das Widget)
    GET /api/equity?bot=wsb&limit=500   Equity-Kurve eines Bots
"""
from __future__ import annotations

import base64
import binascii
import hmac
import os
import sqlite3
from datetime import datetime, timezone
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


def _bot_summary(bot: str) -> dict[str, Any]:
    snaps = _rows(
        "SELECT ts, equity, cash, positions_value, n_positions "
        "FROM snapshots WHERE bot=? ORDER BY ts",
        (bot,),
    )
    if not snaps:
        return {
            "bot": bot,
            "label": LABELS.get(bot, bot),
            "has_data": False,
        }

    equity = [float(r["equity"]) for r in snaps if r["equity"] is not None]
    first, last = snaps[0], snaps[-1]
    start = float(first["equity"])
    end = float(last["equity"])
    ret = (end / start - 1.0) * 100.0 if start else 0.0

    trade_count = _rows("SELECT COUNT(*) AS n FROM trades WHERE bot=?", (bot,))[0]["n"]
    last_dt = _iso(last["ts"])
    now = datetime.now(timezone.utc)
    age_minutes = int((now - last_dt).total_seconds() // 60) if last_dt else None

    return {
        "bot": bot,
        "label": LABELS.get(bot, bot),
        "has_data": True,
        "equity": round(end, 2),
        "return_pct": round(ret, 2),
        "max_drawdown_pct": round(_max_drawdown(equity), 2),
        "trades": int(trade_count),
        "open_positions": int(last["n_positions"] or 0),
        "cash": round(float(last["cash"] or 0), 2),
        "since": (first["ts"] or "")[:10],
        "last_ts": last["ts"],
        "age_minutes": age_minutes,
        "stale": bool(age_minutes is not None and age_minutes > 180),
    }


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
