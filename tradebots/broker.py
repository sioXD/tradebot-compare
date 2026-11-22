"""Duenner Wrapper um alpaca-py (immer paper=True)."""
from __future__ import annotations

import math
import os
import platform
import re
import uuid
from datetime import datetime, timedelta, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest, MarketOrderRequest

from .config import BotConfig

# Kennung dieser Installation (z.B. "server" oder "laptop"). Jede Order bekommt sie als Praefix
# in der client_order_id -> fremde Orders (andere Instanz, manuell im Web) sind erkennbar.
INSTANCE_ID = re.sub(r"[^A-Za-z0-9_]", "", os.getenv("INSTANCE_ID") or platform.node() or "bot")[:24] or "bot"


def new_client_order_id() -> str:
    return f"{INSTANCE_ID}-{uuid.uuid4().hex[:16]}"


def is_own_order(client_order_id: str | None) -> bool:
    return bool(client_order_id) and client_order_id.startswith(f"{INSTANCE_ID}-")


class Broker:
    def __init__(self, cfg: BotConfig):
        if not cfg.alpaca_key or not cfg.alpaca_secret:
            raise RuntimeError(f"ALPACA_{cfg.name.upper()}_KEY/SECRET fehlen in .env")
        # paper=True ist fest verdrahtet -> es kann nie echtes Geld bewegt werden
        self.trading = TradingClient(cfg.alpaca_key, cfg.alpaca_secret, paper=True)
        self.data = StockHistoricalDataClient(cfg.alpaca_key, cfg.alpaca_secret)
        self._asset_cache: dict[str, object] = {}

    def is_open(self) -> bool:
        return bool(self.trading.get_clock().is_open)

    def account(self):
        return self.trading.get_account()

    def positions(self) -> dict:
        return {p.symbol: p for p in self.trading.get_all_positions()}

    def open_order_symbols(self) -> set[str]:
        orders = self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        return {o.symbol for o in orders}

    def asset(self, symbol: str):
        if symbol not in self._asset_cache:
            try:
                self._asset_cache[symbol] = self.trading.get_asset(symbol)
            except Exception:
                self._asset_cache[symbol] = None
        return self._asset_cache[symbol]

    def is_tradable(self, symbol: str) -> bool:
        a = self.asset(symbol)
        return bool(a and a.tradable and a.status == AssetStatus.ACTIVE)

    def latest_price(self, symbol: str) -> float:
        r = self.data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
        return float(r[symbol].price)

    def market_summary(self, symbols: list[str]) -> dict[str, dict]:
        """Liefert letzten Schlusskurs sowie 1-Tages- und 5-Tages-Veraenderung fuer eine Liste von Tickern."""
        syms = list(dict.fromkeys(s.upper() for s in symbols if s))[:25]
        if not syms:
            return {}
        try:
            start = datetime.now(timezone.utc) - timedelta(days=12)
            bars = self.data.get_stock_bars(
                StockBarsRequest(symbol_or_symbols=syms, timeframe=TimeFrame.Day, start=start, feed="iex")
            )
            out: dict[str, dict] = {}
            for sym in syms:
                series = bars.data.get(sym, [])
                if not series:
                    continue
                closes = [float(b.close) for b in series]
                last = closes[-1]
                chg_1d = (last / closes[-2] - 1) if len(closes) >= 2 else 0.0
                ref_5d = closes[-6] if len(closes) >= 6 else closes[0]
                chg_5d = (last / ref_5d - 1) if ref_5d else 0.0
                out[sym] = {"price": last, "chg_1d": chg_1d, "chg_5d": chg_5d}
            return out
        except Exception:  # noqa: BLE001
            return {}

    def buy_notional(self, symbol: str, notional: float):
        """Kauft fuer ~notional USD. Fractional wenn moeglich, sonst ganze Stueck."""
        a = self.asset(symbol)
        coid = new_client_order_id()
        if a is not None and getattr(a, "fractionable", False):
            req = MarketOrderRequest(symbol=symbol, notional=round(notional, 2), side=OrderSide.BUY,
                                     time_in_force=TimeInForce.DAY, client_order_id=coid)
            qty = None
        else:
            price = self.latest_price(symbol)
            qty = math.floor(notional / price)
            if qty < 1:
                raise RuntimeError(f"{symbol}: Preis {price:.2f} > Positionsgroesse {notional:.2f}")
            req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY,
                                     time_in_force=TimeInForce.DAY, client_order_id=coid)
        order = self.trading.submit_order(req)
        return order, qty

    def close(self, symbol: str):
        """Verkauft die ganze Position - als normale Order, damit sie unsere client_order_id traegt."""
        pos = self.trading.get_open_position(symbol)
        try:
            req = MarketOrderRequest(symbol=symbol, qty=abs(float(pos.qty)), side=OrderSide.SELL,
                                     time_in_force=TimeInForce.DAY, client_order_id=new_client_order_id())
            return self.trading.submit_order(req)
        except Exception:  # noqa: BLE001
            return self.trading.close_position(symbol)  # Fallback

    # ---------- Historie direkt von Alpaca (ueberlebt jeden DB-Verlust) ----------
    def filled_orders(self, days: int = 90) -> list:
        after = datetime.now(timezone.utc) - timedelta(days=days)
        orders = self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=after, limit=500))
        return [o for o in orders if float(o.filled_qty or 0) > 0]

    def equity_history(self, start: datetime) -> list[tuple[datetime, float]]:
        """Equity-Verlauf des Kontos: Tageswerte seit start + 15-Minuten-Werte der letzten Woche.
        (Alpaca liefert bei intraday+start teils fehlerhafte Werte, daher period='1W'.)"""
        points: dict[datetime, float] = {}
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        daily = self.trading.get_portfolio_history(GetPortfolioHistoryRequest(start=start, timeframe="1D"))
        base = float(daily.base_value or 0) or None
        for t, e in zip(daily.timestamp, daily.equity):
            ts = datetime.fromtimestamp(t, timezone.utc)
            if e and ts < week_ago:
                points[ts] = float(e)
        intraday = self.trading.get_portfolio_history(GetPortfolioHistoryRequest(period="1W", timeframe="15Min"))
        for t, e in zip(intraday.timestamp, intraday.equity):
            ts = datetime.fromtimestamp(t, timezone.utc)
            if e and ts >= start:
                points[ts] = float(e)
        ref = base or (next(iter(points.values())) if points else None)
        if ref:  # Plausibilitaetsfilter gegen API-Ausreisser
            points = {k: v for k, v in points.items() if 0.5 * ref <= v <= 1.5 * ref}
        if base and points and start < min(points):
            points[start] = base  # Startkapital als Ausgangspunkt der Rendite
        return sorted(points.items())
