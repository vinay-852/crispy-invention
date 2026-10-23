import logging

from ..schemas import ConditionType, Rule, RuleDraft
from .adk import AdkAgent

log = logging.getLogger(__name__)


class WatchCommandParser(AdkAgent):
    """ADK agent that parses free-text /watch commands into a RuleDraft."""

    INSTRUCTION = (
        "You extract stock watch rules from free text. Return ONLY JSON with keys: "
        'symbol (ticker or resolved symbol), '
        'condition_type (one of "price_below", "drop_pct", "rsi_below", "sentiment_positive"), '
        "threshold (number or null), max_amount (number or null). "
        'Example: "watch apple under 180 max 250" -> '
        '{"symbol": "AAPL", "condition_type": "price_below", "threshold": 180, "max_amount": 250}'
    )

    def __init__(self, model: str = "gemini-2.0-flash"):
        super().__init__(name="watch_parser_agent", instruction=self.INSTRUCTION, model=model)

    async def parse(self, text: str) -> RuleDraft | None:
        data = await self.complete_json(text)
        if not data or not data.get("symbol"):
            return None
        try:
            return RuleDraft(
                symbol=str(data["symbol"]).upper(),
                condition_type=ConditionType(data.get("condition_type", "price_below")),
                threshold=data.get("threshold"),
                max_amount=data.get("max_amount"),
            )
        except Exception:
            log.warning("unparseable draft: %s", data)
            return None


class RuleModifier(AdkAgent):
    """ADK agent that applies a user's modification instruction to a rule (the modify/re-trigger loop)."""

    INSTRUCTION = (
        "You adjust an existing stock watch rule based on a user instruction. Return ONLY JSON with keys: "
        "threshold (number or null), max_amount (number or null), "
        'condition_type (one of "price_below", "drop_pct", "rsi_below", "sentiment_positive" or null). '
        "Set a key to null when the user did not change it. "
        "Thresholds are prices in USD, except for drop_pct which is a percentage."
    )

    def __init__(self, model: str = "gemini-2.0-flash"):
        super().__init__(name="rule_modifier_agent", instruction=self.INSTRUCTION, model=model)

    async def modify(self, rule: Rule, instruction: str) -> Rule | None:
        data = await self.complete_json(
            f"Current rule: {rule.model_dump_json()}\nUser instruction: {instruction}")
        if not data:
            return None
        try:
            updates = {k: v for k, v in data.items() if v is not None}
            condition_type = ConditionType(updates["condition_type"]) if "condition_type" in updates else rule.condition_type
            return rule.model_copy(update={
                "condition_type": condition_type,
                "threshold": updates.get("threshold", rule.threshold),
                "max_amount": updates.get("max_amount", rule.max_amount),
            })
        except Exception:
            log.warning("could not apply modification: %s", data)
            return None