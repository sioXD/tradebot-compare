"""Handelslogik - fuer alle Bots identisch, damit der Vergleich fair ist.
Nur die Datenquelle (und damit die Signale) unterscheidet sich.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

from . import db
from .broker import Broker
from .config import BotConfig

log = logging.getLogger(__name__)
REBUY_COOLDOWN = timedelta(hours=24)


def manage_exits(cfg: BotConfig, broker: Broker) -> None:
    for sym, pos in broker.positions().items():
        plpc = float(pos.unrealized_plpc or 0)
        opened = db.last_buy_ts(cfg.name, sym)
        reason = None
        if plpc <= -cfg.stop_loss_pct:
            reason = f"Stop-Loss ({plpc:.1%})"
        elif plpc >= cfg.take_profit_pct:
            reason = f"Take-Profit ({plpc:.1%})"
        elif opened and db.now() - opened > timedelta(days=cfg.max_hold_days):
            reason = f"Max. Haltedauer {cfg.max_hold_days}d erreicht ({plpc:.1%})"
        if reason:
            try:
                order = broker.close(sym)
                db.insert_trade(cfg.name, sym, "sell", float(pos.market_value), float(pos.qty), str(order.id), reason)
                db.log_event(cfg.name, "INFO", f"EXIT {sym}: {reason}")
            except Exception as e:  # noqa: BLE001
                db.log_event(cfg.name, "ERROR", f"Exit {sym} fehlgeschlagen: {e}")


def execute_signals(cfg: BotConfig, broker: Broker) -> None:
    db.expire_signals(cfg.name, cfg.signal_ttl_hours)
    pending = db.pending_signals(cfg.name)
    if not pending:
        return

    by_ticker: dict[str, list] = defaultdict(list)
    for s in pending:
        by_ticker[s["ticker"]].append(s)

    acct = broker.account()
    equity, cash = float(acct.equity), float(acct.cash)
    positions = broker.positions()
    open_orders = broker.open_order_symbols()
    n_pos = len(positions | {s: None for s in open_orders})

    # staerkste Signale zuerst
    def score(sigs):
        buy = max((s["confidence"] for s in sigs if s["action"] == "buy"), default=0)
        sell = max((s["confidence"] for s in sigs if s["action"] == "sell"), default=0)
        return buy, sell

    ranked = sorted(by_ticker.items(), key=lambda kv: max(score(kv[1])), reverse=True)

    for ticker, sigs in ranked:
        ids = [s["id"] for s in sigs]
        buy, sell = score(sigs)
        held = ticker in positions

        if max(buy, sell) < cfg.min_confidence:
            db.set_signal_status(ids, "skipped", f"Confidence {max(buy, sell):.2f} < {cfg.min_confidence}")
            continue

        if sell > buy:
            if held:
                try:
                    pos = positions[ticker]
                    order = broker.close(ticker)
                    db.insert_trade(cfg.name, ticker, "sell", float(pos.market_value), float(pos.qty), str(order.id), f"Sell-Signal ({sell:.2f})", ids)
                    db.set_signal_status(ids, "executed", "Position geschlossen")
                    n_pos -= 1
                except Exception as e:  # noqa: BLE001
                    db.set_signal_status(ids, "error", str(e)[:300])
            else:
                db.set_signal_status(ids, "skipped", "Sell-Signal, aber keine Position (kein Shorting)")
            continue

        # Buy-Signal
        if held or ticker in open_orders:
            db.set_signal_status(ids, "skipped", "Bereits investiert")
            continue
        last_sell = db.last_sell_ts(cfg.name, ticker)
        if last_sell and db.now() - last_sell < REBUY_COOLDOWN:
            db.set_signal_status(ids, "skipped", "Cooldown nach Verkauf")
            continue
        if n_pos >= cfg.max_positions:
            db.set_signal_status(ids, "skipped", f"Max. {cfg.max_positions} Positionen erreicht")
            continue
        if not broker.is_tradable(ticker):
            db.set_signal_status(ids, "skipped", "Bei Alpaca nicht handelbar")
            continue
        notional = equity * cfg.position_pct
        if cash < notional:
            db.set_signal_status(ids, "skipped", "Nicht genug Cash")
            continue
        try:
            order, qty = broker.buy_notional(ticker, notional)
            reasons = " | ".join(s["reason"] for s in sigs if s["action"] == "buy")[:500]
            db.insert_trade(cfg.name, ticker, "buy", notional, qty, str(order.id), f"Buy ({buy:.2f}): {reasons}", ids)
            db.set_signal_status(ids, "executed", f"Kauf ~${notional:,.0f}")
            db.log_event(cfg.name, "INFO", f"BUY {ticker} ~${notional:,.0f} (conf {buy:.2f})")
            cash -= notional
            n_pos += 1
        except Exception as e:  # noqa: BLE001
            db.set_signal_status(ids, "error", str(e)[:300])
            db.log_event(cfg.name, "ERROR", f"Kauf {ticker} fehlgeschlagen: {e}")


def snapshot(cfg: BotConfig, broker: Broker) -> None:
    acct = broker.account()
    positions = broker.positions()
    pv = sum(float(p.market_value) for p in positions.values())
    db.insert_snapshot(cfg.name, float(acct.equity), float(acct.cash), pv, len(positions))
