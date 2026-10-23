import asyncio
import logging

from ..config import Settings
from ..providers.market import MarketData
from ..providers.news import NewsProvider, SentimentAnalyzer
from ..schemas import ConditionType, CriterionScore, ResearchBrief, Rule
from .adk_research import AdkResearchAgent

log = logging.getLogger(__name__)

CONDITION_LABELS = {
    ConditionType.PRICE_BELOW: "price below",
    ConditionType.DROP_PCT: "dropped more than",
    ConditionType.RSI_BELOW: "RSI(14) below",
    ConditionType.SENTIMENT_POSITIVE: "news sentiment positive",
}


class ResearchService:
    """Market + news research → criterion scoring → AI investment brief.

    Data comes only from free keyless endpoints (yfinance, Google News RSS).
    The brief is written by the Google ADK agent; without GOOGLE_API_KEY a
    deterministic fallback keeps the money loop working offline.
    """

    def __init__(self, settings: Settings, market: MarketData, news: NewsProvider,
                 adk: AdkResearchAgent | None = None):
        self.settings = settings
        self.market = market
        self.news = news
        self.adk = adk or AdkResearchAgent(model=settings.adk_model)
        self.sentiment = SentimentAnalyzer()

    async def research(self, rule: Rule) -> ResearchBrief:
        quote = await asyncio.to_thread(self.market.get_quote, rule.symbol)
        price = quote.price if quote.price is not None else 0.0
        criteria: list[CriterionScore] = []

        if rule.condition_type == ConditionType.PRICE_BELOW:
            criteria.append(CriterionScore(
                name="Price", expected=f"< ${rule.threshold:,.2f}", actual=f"${price:,.2f}",
                passed=rule.threshold is not None and price < rule.threshold))
        elif rule.condition_type == ConditionType.DROP_PCT:
            drop = await asyncio.to_thread(self.market.get_drop_pct, rule.symbol)
            criteria.append(CriterionScore(
                name="Drawdown", expected=f"> {rule.threshold:,.0f}% from 52w high",
                actual=f"{drop:.1f}%" if drop is not None else "n/a",
                passed=drop is not None and rule.threshold is not None and drop > rule.threshold))
        elif rule.condition_type == ConditionType.RSI_BELOW:
            rsi = await asyncio.to_thread(self.market.get_rsi, rule.symbol)
            criteria.append(CriterionScore(
                name="RSI(14)", expected=f"< {rule.threshold:,.1f}",
                actual=f"{rsi:.1f}" if rsi is not None else "n/a",
                passed=rsi is not None and rule.threshold is not None and rsi < rule.threshold))

        items = await asyncio.to_thread(self.news.fetch, rule.symbol, 5)
        senti, headlines = self.sentiment.analyze(items)
        if rule.condition_type == ConditionType.SENTIMENT_POSITIVE:
            criteria.append(CriterionScore(
                name="News sentiment", expected="positive",
                actual="positive" if senti > 0 else f"{senti:+.2f}", passed=senti > 0))

        brief = await self._brief(rule, price, criteria, headlines)
        return ResearchBrief(
            symbol=rule.symbol,
            criteria=criteria,
            summary=brief["summary"],
            recommendation_amount=brief["amount"],
            risk_note=brief["risk"],
            sources=headlines[:3],
        )

    async def _brief(self, rule: Rule, price: float, criteria: list[CriterionScore],
                     headlines: list[str]) -> dict:
        pass_ratio = sum(1 for c in criteria if c.passed) / max(1, len(criteria))
        fallback = {
            "summary": (f"{rule.symbol} at ${price:,.2f}: condition "
                        f"'{CONDITION_LABELS[rule.condition_type]}' is "
                        f"{'met' if all(c.passed for c in criteria) else 'partially met'}."),
            "amount": round((rule.max_amount or 300.0) * (0.4 + 0.6 * pass_ratio), 2),
            "risk": "Paper trade on sandbox funds — no real money. Market data may be delayed; verify before acting.",
        }
        if not self.adk.available:
            return fallback
        data = await self.adk.research(rule.symbol, price, criteria, rule.max_amount, headlines)
        if not data:
            return fallback
        try:
            amount = float(data.get("recommendation_amount") or 0)
            if amount <= 0 or (rule.max_amount and amount > rule.max_amount):
                amount = fallback["amount"]
            return {
                "summary": str(data.get("summary") or fallback["summary"]),
                "amount": round(amount, 2),
                "risk": str(data.get("risk_note") or fallback["risk"]),
            }
        except Exception:
            log.exception("bad ADK brief")
            return fallback