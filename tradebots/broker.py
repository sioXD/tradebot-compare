"""Duenner Wrapper um alpaca-py (immer paper=True)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

from .config import BotConfig


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
        if a is not None and getattr(a, "fractionable", False):
            req = MarketOrderRequest(symbol=symbol, notional=round(notional, 2), side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
            qty = None
        else:
            price = self.latest_price(symbol)
            qty = math.floor(notional / price)
            if qty < 1:
                raise RuntimeError(f"{symbol}: Preis {price:.2f} > Positionsgroesse {notional:.2f}")
            req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        order = self.trading.submit_order(req)
        return order, qty

    def close(self, symbol: str):
        return self.trading.close_position(symbol)
