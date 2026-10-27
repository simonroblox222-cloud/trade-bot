"""Execution agent: talks to the Alpaca paper trading API."""
import logging

from .config import Settings

log = logging.getLogger("execution")


class Broker:
    def __init__(self, settings: Settings, dry_run=False):
        from alpaca.trading.client import TradingClient

        settings.require_keys()
        self.client = TradingClient(settings.api_key, settings.secret_key, paper=True)
        self.dry_run = dry_run
        acct = self.client.get_account()
        # extra guard: paper account numbers start with "PA"
        if not str(acct.account_number).startswith("PA"):
            raise SystemExit("Connected account does not look like a paper account. Refusing to trade.")

    def account(self):
        a = self.client.get_account()
        return dict(equity=float(a.equity), cash=float(a.cash), last_equity=float(a.last_equity),
                    buying_power=float(a.buying_power), blocked=a.trading_blocked)

    def peak_equity(self) -> float:
        """Highest daily equity over the last year, from Alpaca, so the kill switch survives restarts."""
        from alpaca.trading.requests import GetPortfolioHistoryRequest

        try:
            h = self.client.get_portfolio_history(GetPortfolioHistoryRequest(period="1A", timeframe="1D"))
            return max([e for e in h.equity if e] or [0.0])
        except Exception as e:  # history is a nice-to-have, never block trading on it
            log.warning("portfolio history unavailable: %s", e)
            return 0.0

    def positions(self) -> dict:
        return {p.symbol: dict(qty=float(p.qty), avg=float(p.avg_entry_price), price=float(p.current_price),
                               pnl=float(p.unrealized_pl)) for p in self.client.get_all_positions()}

    def clock(self):
        return self.client.get_clock()

    def has_open_order(self, symbol) -> bool:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
        return len(orders) > 0

    def buy(self, symbol, qty: int, stop: float):
        """Market buy with an attached GTC protective stop (OTO order)."""
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest

        log.info("BUY %s x%d stop %.2f%s", symbol, qty, stop, " (dry run)" if self.dry_run else "")
        if self.dry_run:
            return None
        req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC,
                                 order_class=OrderClass.OTO, stop_loss=StopLossRequest(stop_price=round(stop, 2)))
        return self.client.submit_order(req)

    def sell_all(self, symbol):
        log.info("SELL %s (close position)%s", symbol, " (dry run)" if self.dry_run else "")
        if self.dry_run:
            return None
        self.cancel_orders(symbol)  # free the shares held by the protective stop
        return self.client.close_position(symbol)

    def cancel_orders(self, symbol):
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        for o in self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])):
            self.client.cancel_order_by_id(o.id)

    def flatten(self):
        log.warning("FLATTEN all positions%s", " (dry run)" if self.dry_run else "")
        if not self.dry_run:
            self.client.close_all_positions(cancel_orders=True)
