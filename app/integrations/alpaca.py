from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from ..config import Settings


class AlpacaError(RuntimeError):
    pass


class AlpacaClient:
    """Alpaca paper trading — fractional market orders via notional amounts."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None
        if settings.alpaca_api_key and settings.alpaca_secret_key:
            self._client = TradingClient(settings.alpaca_api_key, settings.alpaca_secret_key, paper=True)

    @property
    def available(self) -> bool:
        return self._client is not None

    def submit_market_order(self, symbol: str, notional: float):
        if not self.available:
            raise AlpacaError("Alpaca credentials not configured (ALPACA_API_KEY / ALPACA_SECRET_KEY)")
        req = MarketOrderRequest(symbol=symbol, notional=notional, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        order = self._client.submit_order(req)
        if order.status != OrderStatus.FILLED:
            raise AlpacaError(f"Alpaca order not filled (status={order.status})")
        return order

    def get_last_trade_price(self, symbol: str) -> float:
        if not self.available:
            raise AlpacaError("Alpaca credentials not configured (ALPACA_API_KEY / ALPACA_SECRET_KEY)")
        return float(self._client.get_latest_trade(symbol).price)