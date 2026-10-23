import json

from .adk import AdkAgent

BRIEF_INSTRUCTION = (
    "You are a cautious stock research assistant for a retail investor. "
    "You are given a symbol, its current price, the user's watch criteria with "
    "pass/fail scores, and fresh news headlines. Write an investment brief as "
    "ONE JSON object (no markdown fences, no prose around it) with exactly "
    'these keys: "summary" (2-3 plain-text sentences), "recommendation_amount" '
    "(a number between 10 and max_amount; 0 means do not invest), "
    '"risk_note" (1 sentence).'
)


class AdkResearchAgent(AdkAgent):
    """Google ADK agent that writes the investment brief from free-endpoint data.

    No tools: the market facts come from yfinance and the headlines come from
    the Google News RSS feed (both keyless), so the brief never depends on a
    search tool.
    """

    def __init__(self, model: str = "gemini-2.0-flash"):
        super().__init__(name="stock_research_agent", instruction=BRIEF_INSTRUCTION, model=model)

    async def research(self, symbol: str, price: float, criteria: list,
                       max_amount: float | None, headlines: list[str]) -> dict | None:
        prompt = json.dumps({
            "symbol": symbol,
            "price": price,
            "criteria": [{"name": c.name, "expected": c.expected, "actual": c.actual, "passed": c.passed}
                         for c in criteria],
            "max_amount": max_amount,
            "headlines": headlines,
        })
        return await self.complete_json(prompt)