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
import sys
import time
from datetime import datetime, timedelta, timezone

from . import db, engine, llm
from .broker import Broker
from .config import bot_config
from .sources import SOURCES

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

BATCH = 30


def fetch_and_analyze(cfg, broker: Broker, log: logging.Logger) -> None:
    items = SOURCES[cfg.name]()
    new = db.insert_items(cfg.name, items)
    if not new:
        log.info("Keine neuen Posts")
        return
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg.max_item_age_hours)
    fresh = [i for i in new if datetime.fromisoformat(i["created_utc"]) >= cutoff]
    stale = [i["item_id"] for i in new if i not in fresh]
    if stale:
        db.mark_analyzed(cfg.name, stale, value=-1)  # zu alt, nicht auswerten
    log.info("%d neue Posts (%d frisch, %d zu alt)", len(new), len(fresh), len(stale))

    for k in range(0, len(fresh), BATCH):
        batch = fresh[k : k + BATCH]
        signals = llm.analyze(cfg.name, batch, cfg.universe)
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
    log.info("Bot gestartet: %s", cfg)
    db.log_event(bot, "INFO", "Bot gestartet")

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
                engine.manage_exits(cfg, broker)
                engine.execute_signals(cfg, broker)
            snap_every = 15 * 60 if is_open else 60 * 60
            if t - last_snap >= snap_every:
                last_snap = t
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
