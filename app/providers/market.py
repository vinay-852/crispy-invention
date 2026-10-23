from dataclasses import dataclass

import yfinance as yf

from ..schemas import SearchResult


@dataclass
class Quote:
    symbol: str
    price: float | None = None
    change_pct: float | None = None


@dataclass
class StockDetail:
    symbol: str
    name: str
    exchange: str
    sector: str
    price: float | None
    change_pct: float | None
    high_52w: float | None
    low_52w: float | None
    pe: float | None
    market_cap: float | None


class MarketData:
    """yfinance wrapper — the single swap point for market data.

    (ponytail: yfinance is an unofficial API and may break; replace this class
    behind the same interface with any provider.)
    """

    def search(self, query: str, max_results: int = 20) -> list[SearchResult]:
        res = yf.Search(query, max_results=max_results)
        out = []
        for q in (res.quotes or []):
            if q.get("quoteType") != "EQUITY":
                continue
            out.append(SearchResult(
                symbol=q["symbol"],
                name=q.get("shortname") or q.get("longname") or "",
                exchange=q.get("exchange") or "",
                price=q.get("regularMarketPrice"),
                change_pct=q.get("regularMarketChangePercent"),
                score=q.get("score") or 0.0,
            ))
        out.sort(key=lambda r: r.score, reverse=True)
        return out

    def get_quote(self, symbol: str) -> Quote:
        info = yf.Ticker(symbol).fast_info
        price = info.get("last_price")
        prev = info.get("previous_close")
        change = None
        if price is not None and prev not in (None, 0):
            change = (price / prev - 1) * 100
        return Quote(symbol=symbol, price=price, change_pct=change)

    def get_detail(self, symbol: str) -> StockDetail:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        fi = ticker.fast_info
        price = fi.get("last_price")
        prev = fi.get("previous_close")
        change = None
        if price is not None and prev not in (None, 0):
            change = (price / prev - 1) * 100
        return StockDetail(
            symbol=symbol,
            name=info.get("longName") or symbol,
            exchange=info.get("exchange") or "",
            sector=info.get("sector") or "",
            price=price,
            change_pct=change,
            high_52w=fi.get("year_high") or info.get("fiftyTwoWeekHigh"),
            low_52w=fi.get("year_low") or info.get("fiftyTwoWeekLow"),
            pe=info.get("trailingPE"),
            market_cap=fi.get("market_cap") or info.get("marketCap"),
        )

    def get_rsi(self, symbol: str, period: int = 14) -> float | None:
        hist = yf.Ticker(symbol).history(period="3mo", interval="1d")
        close = hist["Close"].dropna()
        if len(close) < period + 1:
            return None
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
        rs = gain / loss.replace(0, 1e-9)
        rsi = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1])

    def get_drop_pct(self, symbol: str) -> float | None:
        """Drawdown from the 52-week high, as a positive percentage."""
        quote = self.get_quote(symbol)
        high = yf.Ticker(symbol).fast_info.get("year_high")
        if quote.price is None or not high:
            return None
        return (1 - quote.price / high) * 100