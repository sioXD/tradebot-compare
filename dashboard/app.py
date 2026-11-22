"""Web-Dashboard:  streamlit run dashboard/app.py"""
from __future__ import annotations

import base64
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tradebots import config  # noqa: E402

LABELS = {"wsb": "WallStreetBets", "truth": "Truth Social", "armstrong": "Armstrong Economics", "SPY": "SPY (Benchmark)"}
FAVICON_PATH = Path(__file__).resolve().parent / "favicon.ico"

st.set_page_config(
    page_title="Tradingbot-Vergleich",
    page_icon=str(FAVICON_PATH) if FAVICON_PATH.exists() else None,
    layout="wide",
)


def q(sql: str, params=()) -> pd.DataFrame:
    if not config.DB_PATH.exists():
        return pd.DataFrame()
    with sqlite3.connect(config.DB_PATH, timeout=30) as c:
        return pd.read_sql_query(sql, c, params=params)


@st.cache_data(ttl=3600)
def spy_series(start_iso: str) -> pd.Series:
    """SPY-Tagesschlusskurse seit Start (ueber Alpaca-Daten-API)."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        cfg = next(c for c in (config.bot_config(b) for b in config.BOT_NAMES) if c.alpaca_key)
        client = StockHistoricalDataClient(cfg.alpaca_key, cfg.alpaca_secret)
        start = datetime.fromisoformat(start_iso) - timedelta(days=1)
        bars = client.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame.Day, start=start, feed="iex")).df
        s = bars.reset_index().set_index("timestamp")["close"]
        return s
    except Exception:  # noqa: BLE001
        return pd.Series(dtype=float)


def max_drawdown(eq: pd.Series) -> float:
    if eq.empty:
        return 0.0
    return float((eq / eq.cummax() - 1).min())

if FAVICON_PATH.exists():
    icon_b64 = base64.b64encode(FAVICON_PATH.read_bytes()).decode("ascii")
    st.markdown(
        f"""<div style="display:flex;align-items:center;gap:14px;margin-bottom:0.5rem;">
            <img src="data:image/x-icon;base64,{icon_b64}" width="40" height="40" alt="Logo" />
            <h1 style="margin:0;padding:0;">Tradingbot-Vergleich (Paper Trading)</h1>
        </div>""",
        unsafe_allow_html=True,
    )
else:
    st.title("Tradingbot-Vergleich (Paper Trading)")

if st.button("Aktualisieren", icon=":material/refresh:"):
    st.cache_data.clear()

snaps = q("SELECT * FROM snapshots ORDER BY ts")
if not snaps.empty:
    snaps["ts"] = pd.to_datetime(snaps["ts"], utc=True)
trades = q("SELECT * FROM trades ORDER BY ts DESC")

# Startzeitpunkt des Experiments: EXPERIMENT_START (.env, z.B. 2026-11-20) > erster Snapshot > vor 30 Tagen
_start_env = os.getenv("EXPERIMENT_START", "").strip()
if _start_env:
    START = pd.Timestamp(_start_env, tz="UTC")
elif not snaps.empty:
    START = snaps["ts"].min().normalize()
elif not trades.empty:
    START = pd.to_datetime(trades["ts"], utc=True).min().normalize()
else:
    START = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=30)


@st.cache_data(ttl=300)
def alpaca_equity(bot: str, start_iso: str) -> pd.Series:
    """Equity-Verlauf direkt vom Alpaca-Konto - unabhaengig von der lokalen DB."""
    try:
        from tradebots.broker import Broker

        cfg = config.bot_config(bot)
        if not cfg.alpaca_key:
            return pd.Series(dtype=float)
        pts = Broker(cfg).equity_history(datetime.fromisoformat(start_iso))
        return pd.Series({ts: eq for ts, eq in pts}, dtype=float).sort_index()
    except Exception:  # noqa: BLE001
        return pd.Series(dtype=float)


@st.cache_data(ttl=60)
def alpaca_account(bot: str) -> dict:
    try:
        from tradebots.broker import Broker

        b = Broker(config.bot_config(bot))
        a = b.account()
        return {"equity": float(a.equity), "cash": float(a.cash), "n_positions": len(b.positions())}
    except Exception:  # noqa: BLE001
        return {}


# ---- Performance-Kurve (in % seit Start) ----
curves = {}
summary = []
for bot in config.BOT_NAMES:
    s = alpaca_equity(bot, START.isoformat())
    source = "Alpaca"
    if s.empty and not snaps.empty:  # Fallback: lokale Snapshots
        s = snaps[snaps.bot == bot].set_index("ts")["equity"]
        source = "lokale DB"
    if s.empty:
        continue
    acct = alpaca_account(bot)
    if acct:  # aktuellen Stand anhaengen, damit die Kurve bis "jetzt" reicht
        s = pd.concat([s, pd.Series({pd.Timestamp.now(tz="UTC"): acct["equity"]})])
    curves[LABELS[bot]] = (s / s.iloc[0] - 1) * 100
    n_trades = int((trades.bot == bot).sum()) if not trades.empty else 0
    last = snaps[snaps.bot == bot].iloc[-1] if not snaps.empty and (snaps.bot == bot).any() else None
    summary.append(
        {
            "Bot": LABELS[bot],
            "Equity $": round(acct.get("equity", s.iloc[-1]), 2),
            "Rendite %": round((s.iloc[-1] / s.iloc[0] - 1) * 100, 2),
            "Max Drawdown %": round(max_drawdown(s) * 100, 2),
            "Trades": n_trades,
            "Offene Positionen": acct.get("n_positions", int(last["n_positions"]) if last is not None else 0),
            "Cash $": round(acct.get("cash", float(last["cash"]) if last is not None else 0.0), 2),
            "Seit": s.index[0].strftime("%d.%m.%Y"),
            "Quelle": source,
        }
    )

if not curves:
    st.info("Noch keine Daten. Laufen die Bots und sind die Alpaca-Keys in der .env gesetzt?")
    st.stop()

spy = spy_series(START.isoformat())
if not spy.empty:
    spy = spy[spy.index >= START]
    if not spy.empty:
        curves[LABELS["SPY"]] = (spy / spy.iloc[0] - 1) * 100
        summary.append({"Bot": LABELS["SPY"], "Rendite %": round(float(curves[LABELS["SPY"]].iloc[-1]), 2),
                        "Max Drawdown %": round(max_drawdown(spy) * 100, 2)})

c1, c2 = st.columns([3, 2])
with c1:
    st.subheader("Rendite seit Start (%)")
    df = pd.DataFrame(curves).sort_index().ffill()
    st.line_chart(df)
with c2:
    st.subheader("Kennzahlen")
    st.dataframe(pd.DataFrame(summary).set_index("Bot"), width="stretch")

# ---- Details pro Bot ----
tabs = st.tabs([LABELS[b] for b in config.BOT_NAMES] + ["Log"])
for tab, bot in zip(tabs, config.BOT_NAMES):
    with tab:
        a, b = st.columns(2)
        with a:
            st.markdown("**Trades**")
            t = trades[trades.bot == bot][["ts", "ticker", "side", "notional", "reason"]] if not trades.empty else pd.DataFrame()
            st.dataframe(t, width="stretch", hide_index=True, height=300)
        with b:
            st.markdown("**Signale**")
            sig = q("SELECT created_at, ticker, action, confidence, status, status_note, reason FROM signals WHERE bot=? ORDER BY id DESC LIMIT 200", (bot,))
            st.dataframe(sig, width="stretch", hide_index=True, height=300)
        st.markdown("**Letzte Posts der Quelle**")
        items = q("SELECT created_utc, author, title, url, analyzed FROM items WHERE bot=? ORDER BY created_utc DESC LIMIT 50", (bot,))
        st.dataframe(items, width="stretch", hide_index=True, column_config={"url": st.column_config.LinkColumn("url")})

with tabs[-1]:
    ev = q("SELECT ts, bot, level, message FROM events ORDER BY id DESC LIMIT 300")
    st.dataframe(ev, width="stretch", hide_index=True, height=600)

st.caption(f"Stand: {datetime.now(timezone.utc).astimezone().strftime('%d.%m.%Y %H:%M')} · Nur Paper Trading, keine Anlageberatung.")
