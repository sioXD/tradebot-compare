"""Startet EINEN Bot:  python -m tradebots.runner <wsb|truth|armstrong>

Ablauf (Endlosschleife):
  - alle FETCH_MINUTES: Quelle abrufen -> neue Posts per LLM auswerten -> Signale speichern
  - alle TRADE_CHECK_MINUTES (nur wenn US-Boerse offen): Exits pruefen, Signale ausfuehren
  - Equity-Snapshot (alle 15 min waehrend Handelszeit, sonst stuendlich)
Nachts ausgeschalteter Server ist kein Problem: verpasste Posts werden beim Start nachgeholt
(soweit juenger als MAX_ITEM_AGE_HOURS), Signale ausserhalb der Handelszeit warten in der Queue.
"""
from __future__ import annotations

import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from . import db, engine, llm, sources
from .broker import INSTANCE_ID, Broker
from .config import BotConfig, bot_config
from .sources import SOURCES

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

BATCH = 30
_TICKER_RE = re.compile(r"(?:\$|#\d+\s+)([A-Z]{1,5})\b")


def build_context(cfg: BotConfig, broker: Broker, batch: list[dict]) -> str:
    """Stellt Depot, Marktkurse, letzte Signale und bisherige Posts dieses Bots fuer die KI zusammen."""
    sections: list[str] = []
    held_syms: list[str] = []

    # 1. Aktuelles Depot
    try:
        acct = broker.account()
        positions = broker.positions()
        held_syms = list(positions.keys())
        pos_lines = []
        for sym, pos in sorted(positions.items()):
            plpc = float(pos.unrealized_plpc or 0)
            mv = float(pos.market_value or 0)
            meta = db.get_position_meta(cfg.name, sym)
            opened = datetime.fromisoformat(meta["opened_at"]) if meta else db.last_buy_ts(cfg.name, sym)
            age = f"{engine._trading_days(opened, db.now())} Handelstage" if opened else "?"
            peak_txt = f", Peak {float(meta['peak_plpc']):+.1%}" if meta and float(meta["peak_plpc"] or 0) > 0 else ""
            pos_lines.append(f"  - {sym}: ${mv:,.0f}, P/L {plpc:+.1%}{peak_txt}, gehalten {age}")
        header = f"Portfolio (Equity ${float(acct.equity):,.0f}, Cash ${float(acct.cash):,.0f}, {len(positions)}/{cfg.max_positions} Positionen):"
        sections.append(header + ("\n" + "\n".join(pos_lines) if pos_lines else " keine offenen Positionen"))
    except Exception:  # noqa: BLE001
        pass

    # 2. Marktlage & relevante Kurse (Benchmarks + Depot + im Batch genannte Ticker)
    mentioned: list[str] = []
    for it in batch:
        txt = f"{it.get('title', '')} {it.get('text', '')}"
        mentioned.extend(_TICKER_RE.findall(txt))
    watch = ["SPY", "QQQ", "GLD", "TLT"] + held_syms + mentioned[:10] + cfg.universe[:8]
    summary = broker.market_summary(watch)
    if summary:
        m_lines = [f"  - {sym}: ${d['price']:.2f} (1d {d['chg_1d']:+.1%}, 5d {d['chg_5d']:+.1%})" for sym, d in summary.items()]
        sections.append("Marktkurse:\n" + "\n".join(m_lines))

    # 3. Eigene Signal-Historie
    sigs = db.recent_signals(cfg.name, limit=12)
    if sigs:
        s_lines = [
            f"  - {r['created_at'][:16]} {r['action'].upper()} {r['ticker']} (conf {r['confidence']:.2f}, {r['status']}): {(r['reason'] or '')[:120]}"
            for r in sigs
        ]
        sections.append("Letzte eigene Signale:\n" + "\n".join(s_lines))

    # 4. Gedaechtnis: vorherige ausgewertete Posts dieser Quelle
    batch_ids = {it["item_id"] for it in batch}
    prev = db.recent_items(cfg.name, limit=12, exclude_ids=batch_ids)
    if prev:
        p_lines = []
        for r in prev:
            snippet = (r["text"] or "").replace("\n", " ")[:180]
            p_lines.append(f"  - [{(r['created_utc'] or '')[:16]}] {r['title'] or ''} — {snippet}")
        sections.append("Vorherige Posts dieser Quelle:\n" + "\n".join(p_lines))

    return "\n\n".join(sections)


def fetch_and_analyze(cfg, broker: Broker, log: logging.Logger) -> None:
    items = SOURCES[cfg.name]()
    new = db.insert_items(cfg.name, items)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg.max_item_age_hours)
    cutoff_iso = cutoff.isoformat()

    # Ueberalterte oder inhaltsleere Posts (z.B. leere Medien-Posts, Paywall-Teaser) direkt als -1 markieren
    db.mark_stale_unanalyzed(cfg.name, cutoff_iso)
    junk = [i["item_id"] for i in new if sources.is_junk_item(cfg.name, i)]
    if junk:
        db.mark_analyzed(cfg.name, junk, value=-1)

    # Alle frischen noch nicht analysierten Items holen (holt auch fehlgeschlagene LLM-Batches nach)
    fresh = db.unanalyzed_items(cfg.name, cutoff_iso)
    if not fresh:
        log.info("Keine neuen analysierbaren Posts (neu=%d, uebersprungen=%d)", len(new), len(junk))
        return
    log.info("%d Posts zur Analyse (%d neu, %d uebersprungen)", len(fresh), len(new), len(junk))

    for k in range(0, len(fresh), BATCH):
        batch = fresh[k : k + BATCH]
        ctx = build_context(cfg, broker, batch)
        signals = llm.analyze(cfg.name, batch, cfg.universe, context=ctx)
        ids = [i["item_id"] for i in batch]
        for s in signals:
            if not broker.is_tradable(s["ticker"]):
                log.info("Ignoriere %s (nicht handelbar)", s["ticker"])
                continue
            db.insert_signal(cfg.name, s, s.get("post_ids") or ids)
            db.log_event(cfg.name, "INFO", f"Signal {s['action'].upper()} {s['ticker']} conf={s['confidence']:.2f}: {s['reason']}")
        db.mark_analyzed(cfg.name, ids)
        log.info("Batch %d Posts -> %d Signale", len(batch), len(signals))


def main(bot: str) -> None:
    log = logging.getLogger(bot)
    cfg = bot_config(bot)
    db.init()
    broker = Broker(cfg)
    started_at = db.now()
    log.info("Bot gestartet (Instanz '%s'): %s", INSTANCE_ID, cfg)
    db.log_event(bot, "INFO", f"Bot gestartet (Instanz '{INSTANCE_ID}')")

    paused = False

    def sync() -> None:
        nonlocal paused
        foreign = engine.sync_orders(cfg, broker, started_at)
        if foreign and not paused:
            paused = True
            msg = (f"HANDEL PAUSIERT: Auf diesem Alpaca-Konto wurden Orders erteilt, die nicht von Instanz "
                   f"'{INSTANCE_ID}' stammen ({', '.join(foreign)}). Laeuft der Bot noch auf einem zweiten Rechner? "
                   f"Andere Instanz stoppen und diesen Container neu starten.")
            log.error(msg)
            db.log_event(bot, "ERROR", msg)

    try:
        sync()  # Historie von Alpaca nachtragen (falls DB neu/unvollstaendig)
    except Exception as e:  # noqa: BLE001
        db.log_event(bot, "ERROR", f"Order-Sync: {e}")

    last_fetch = last_trade = last_snap = 0.0
    while True:
        t = time.time()
        try:
            if t - last_fetch >= cfg.fetch_minutes * 60:
                last_fetch = t
                fetch_and_analyze(cfg, broker, log)
        except Exception as e:  # noqa: BLE001
            log.exception("Fetch/Analyse fehlgeschlagen")
            db.log_event(bot, "ERROR", f"Fetch/Analyse: {e}")

        try:
            is_open = broker.is_open()
            if is_open and t - last_trade >= cfg.trade_check_minutes * 60:
                last_trade = t
                sync()
                if not paused:
                    engine.manage_exits(cfg, broker)
                    engine.execute_signals(cfg, broker)
            snap_every = 15 * 60 if is_open else 60 * 60
            if t - last_snap >= snap_every:
                last_snap = t
                if not is_open:
                    sync()
                engine.snapshot(cfg, broker)
        except Exception as e:  # noqa: BLE001
            log.exception("Trading-Schritt fehlgeschlagen")
            db.log_event(bot, "ERROR", f"Trading: {e}")

        time.sleep(30)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m tradebots.runner <wsb|truth|armstrong>")
        sys.exit(1)
    main(sys.argv[1])
