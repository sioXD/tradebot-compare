"""Handelslogik - fuer alle Bots identisch, damit der Vergleich fair ist.
Nur die Datenquelle (und damit die Signale) unterscheidet sich.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from . import config, db
from .broker import Broker, is_own_order
from .config import BotConfig

log = logging.getLogger(__name__)
REBUY_COOLDOWN = timedelta(hours=24)


def _trading_days(start: datetime, end: datetime) -> int:
    """Zaehlt Werktage (Mo-Fr) zwischen start und end, damit Wochenenden die Haltedauer nicht verkuerzen."""
    d, end_d = start.date(), end.date()
    if end_d <= d:
        return 0
    days = 0
    cur = d + timedelta(days=1)
    while cur <= end_d:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


def _position_notional(cfg: BotConfig, equity: float, conf: float) -> float:
    """Confidence-gewichtete Positionsgroesse: 0.65 -> 10%, 0.72+ -> 15%, 0.80+ -> 20%."""
    if conf >= 0.80:
        pct = min(cfg.position_pct * 2.0, 0.25)
    elif conf >= 0.72:
        pct = min(cfg.position_pct * 1.5, 0.20)
    else:
        pct = cfg.position_pct
    return equity * pct


def _score_signals(sigs: list) -> tuple[str, float, object]:
    """Neuestes Signal gibt die Richtung vor; aeltere gleichgerichtete Signale koennen nur leicht bestaetigen
    (max +0.03), aber kein veraltetes 0.65 ueber ein schwaches aktuelles 0.53 heben."""
    latest = sigs[-1]
    action = latest["action"]
    latest_conf = float(latest["confidence"] or 0.0)
    same = [float(s["confidence"] or 0.0) for s in sigs if s["action"] == action]
    eff_conf = min(latest_conf + 0.03, 0.7 * latest_conf + 0.3 * max(same))
    return action, eff_conf, latest


def sync_orders(cfg: BotConfig, broker: Broker, started_at: datetime) -> list[str]:
    """Traegt alle ausgefuehrten Alpaca-Orders nach, die in der lokalen DB fehlen.
    Gibt die Ticker fremder Orders zurueck, die NACH dem Start dieser Instanz erteilt wurden
    (= eine andere Instanz oder ein Mensch handelt gerade auf diesem Konto)."""
    foreign_now: list[str] = []
    for o in broker.filled_orders():
        oid = str(o.id)
        if db.has_trade(cfg.name, oid):
            continue
        own = is_own_order(o.client_order_id)
        qty, px = float(o.filled_qty or 0), float(o.filled_avg_price or 0)
        side = o.side.value if hasattr(o.side, "value") else str(o.side)
        note = "Von Alpaca nachgetragen" + ("" if own else " (Order stammt nicht von dieser Instanz)")
        db.insert_trade(cfg.name, o.symbol, side, qty * px, qty, oid, note, ts=o.filled_at or o.submitted_at)
        if not own and o.submitted_at and o.submitted_at > started_at:
            foreign_now.append(f"{side.upper()} {o.symbol}")
    return foreign_now


def manage_exits(cfg: BotConfig, broker: Broker) -> None:
    positions = broker.positions()
    db.prune_positions_meta(cfg.name, set(positions.keys()))
    now = db.now()

    for sym, pos in positions.items():
        plpc = float(pos.unrealized_plpc or 0)
        meta = db.get_position_meta(cfg.name, sym)
        if meta is None:
            opened = db.last_buy_ts(cfg.name, sym) or now
            peak = max(0.0, plpc)
            horizon = cfg.max_hold_days
            db.upsert_position_meta(cfg.name, sym, opened, peak, horizon)
        else:
            opened = datetime.fromisoformat(meta["opened_at"])
            peak = max(float(meta["peak_plpc"] or 0.0), plpc)
            if peak > float(meta["peak_plpc"] or 0.0):
                db.update_position_peak(cfg.name, sym, peak)
            horizon = int(meta["horizon_days"] or cfg.max_hold_days)

        held_days = _trading_days(opened, now)

        # Gestufter Stop: Hard Stop -> ab +4% Break-Even (+0.5%) -> ab +8% Trailing Stop (5 pp unter Peak)
        stop_floor = -cfg.stop_loss_pct
        stop_label = f"Stop-Loss ({plpc:+.1%})"
        if peak >= cfg.trailing_trigger_pct:
            stop_floor = max(cfg.breakeven_floor_pct, peak - cfg.trailing_stop_pct)
            stop_label = f"Trailing-Stop ({plpc:+.1%}, Peak {peak:+.1%})"
        elif peak >= cfg.breakeven_trigger_pct:
            stop_floor = cfg.breakeven_floor_pct
            stop_label = f"Break-Even-Stop ({plpc:+.1%}, Peak {peak:+.1%})"

        # Zeit-Exit: nur bei stagnierenden/schwachen Positionen; Gewinner (>= +3%) laufen mit engerem Trailing weiter
        reason = None
        if held_days >= horizon:
            if plpc < cfg.winner_keep_pct:
                reason = f"Haltedauer {horizon} Handelstage erreicht ({plpc:+.1%})"
            else:
                tight_floor = max(0.01, peak - 0.04)
                if tight_floor > stop_floor:
                    stop_floor = tight_floor
                    stop_label = f"Gewinner-Trailing nach {held_days}d ({plpc:+.1%}, Peak {peak:+.1%})"

        if reason is None:
            if plpc <= stop_floor:
                reason = stop_label
            elif cfg.take_profit_pct > 0 and plpc >= cfg.take_profit_pct:
                reason = f"Take-Profit ({plpc:+.1%})"

        if reason:
            try:
                order = broker.close(sym)
                db.insert_trade(cfg.name, sym, "sell", float(pos.market_value), float(pos.qty), str(order.id), reason)
                db.delete_position_meta(cfg.name, sym)
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

    # Neue Kaeufe erst nach Opening-Delay (z.B. 15 Min. nach 09:30 NY), Verkaeufe sofort
    mins_open = broker.minutes_since_open()
    buys_allowed = mins_open is None or mins_open >= cfg.opening_delay_minutes

    scored = [(ticker, sigs, *_score_signals(sigs)) for ticker, sigs in by_ticker.items()]
    ranked = sorted(scored, key=lambda x: x[3], reverse=True)

    for ticker, sigs, action, conf, latest in ranked:
        ids = [s["id"] for s in sigs]
        held = ticker in positions

        if cfg.name == "wsb" and action == "buy" and ticker in config.WSB_BLACKLIST:
            db.set_signal_status(ids, "skipped", "Blacklist (Wortkollision)")
            continue

        if conf < cfg.min_confidence:
            db.set_signal_status(ids, "skipped", f"Confidence {conf:.2f} < {cfg.min_confidence}")
            continue

        if action == "sell":
            if held:
                try:
                    pos = positions[ticker]
                    order = broker.close(ticker)
                    db.insert_trade(cfg.name, ticker, "sell", float(pos.market_value), float(pos.qty), str(order.id), f"Sell-Signal ({conf:.2f})", ids)
                    db.delete_position_meta(cfg.name, ticker)
                    db.set_signal_status(ids, "executed", "Position geschlossen")
                    db.log_event(cfg.name, "INFO", f"SELL {ticker} (conf {conf:.2f})")
                    n_pos -= 1
                except Exception as e:  # noqa: BLE001
                    db.set_signal_status(ids, "error", str(e)[:300])
            else:
                db.set_signal_status(ids, "skipped", "Sell-Signal, aber keine Position (kein Shorting)")
            continue

        # Buy-Signal
        if not buys_allowed:
            continue  # bleibt pending bis Opening-Delay vorbei ist
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

        target = _position_notional(cfg, equity, conf)
        min_notional = equity * cfg.position_pct * 0.5
        if cash < min_notional:
            db.set_signal_status(ids, "skipped", "Nicht genug Cash")
            continue
        notional = min(target, cash * 0.98)

        try:
            order, qty = broker.buy_notional(ticker, notional)
            reason_txt = (latest["reason"] or "")[:450]
            horizon = max(1, min(int(latest["horizon_days"] or cfg.max_hold_days), cfg.max_hold_days))
            db.insert_trade(cfg.name, ticker, "buy", notional, qty, str(order.id), f"Buy ({conf:.2f}): {reason_txt}", ids)
            db.upsert_position_meta(cfg.name, ticker, db.now(), 0.0, horizon)
            db.set_signal_status(ids, "executed", f"Kauf ~${notional:,.0f} ({horizon}d)")
            db.log_event(cfg.name, "INFO", f"BUY {ticker} ~${notional:,.0f} (conf {conf:.2f}, Horizont {horizon}d)")
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
