import asyncio
import logging
from datetime import datetime, timedelta, timezone

from ..config import Settings
from ..db import Database
from ..integrations.paypal import PayPalClient
from ..providers.market import MarketData
from ..providers.news import NewsProvider
from ..schemas import ConditionType, ProposalStatus, Rule, RuleStatus
from .research import ResearchService

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MonitorService:
    """Polls active rules (PTB JobQueue every N minutes); fires research → proposal → invoice."""

    def __init__(self, settings: Settings, db: Database, market: MarketData, news: NewsProvider,
                 research: ResearchService, paypal: PayPalClient, bot):
        self.settings = settings
        self.db = db
        self.market = market
        self.news = news
        self.research = research
        self.paypal = paypal
        self.bot = bot
        self.last_check_at: str | None = None

    @property
    def next_check_at(self) -> str | None:
        if not self.last_check_at:
            return None
        return (datetime.fromisoformat(self.last_check_at)
                + timedelta(minutes=self.settings.poll_interval_minutes)).isoformat(timespec="seconds")

    async def check_all(self) -> list[int]:
        self.last_check_at = _now_iso()
        await self._expire_stale_proposals()
        now = _now_iso()
        fired: list[int] = []
        for rule in self.db.active_rules():
            if rule.cooldown_until and rule.cooldown_until > now:
                continue
            try:
                if await self.evaluate(rule):
                    fired.append(rule.id)
                    await self.trigger(rule)
            except Exception as e:
                log.exception("rule %s evaluation failed", rule.id)
                await self.bot.notify_error(rule.chat_id, f"⚠️ Could not check {rule.symbol}: {e}")
        return fired

    async def evaluate(self, rule: Rule) -> bool:
        if rule.condition_type == ConditionType.NOTIFY:
            return True
        if rule.condition_type == ConditionType.PRICE_BELOW:
            q = await asyncio.to_thread(self.market.get_quote, rule.symbol)
            return q.price is not None and rule.threshold is not None and q.price < rule.threshold
        if rule.condition_type == ConditionType.DROP_PCT:
            drop = await asyncio.to_thread(self.market.get_drop_pct, rule.symbol)
            return drop is not None and rule.threshold is not None and drop > rule.threshold
        if rule.condition_type == ConditionType.RSI_BELOW:
            rsi = await asyncio.to_thread(self.market.get_rsi, rule.symbol)
            return rsi is not None and rule.threshold is not None and rsi < rule.threshold
        if rule.condition_type == ConditionType.SENTIMENT_POSITIVE:
            items = await asyncio.to_thread(self.news.fetch, rule.symbol, 5)
            score, _ = self.research.sentiment.analyze(items)
            return score > 0
        return False

    async def trigger(self, rule: Rule) -> None:
        now = _now_iso()
        cooldown_until = (datetime.now(timezone.utc)
                          + timedelta(hours=self.settings.cooldown_hours)).isoformat(timespec="seconds")
        self.db.update_rule(rule.id, status=RuleStatus.TRIGGERED,
                            last_triggered_at=now, cooldown_until=cooldown_until)

        if rule.condition_type == ConditionType.NOTIFY:
            await self._notify_alert(rule)
            self.db.update_rule(rule.id, status=RuleStatus.ACTIVE)
            return

        brief = await self.research.research(rule)
        user = self.db.get_user(rule.chat_id)
        budget = user.budget if user else 1000.0
        max_amt = min(rule.max_amount or budget, budget)
        amount = round(min(brief.recommendation_amount or max_amt, max_amt), 2)
        if amount < 1:
            amount = round(min(10.0, max_amt), 2)

        description = self._brief_text(rule, brief)
        invoice_id, pay_link = await self.paypal.create_invoice_and_send(rule.symbol, amount, description)
        proposal = self.db.create_proposal(rule.chat_id, rule.id, rule.symbol, amount,
                                           invoice_id, pay_link, brief.model_dump_json())
        await self.bot.send_proposal(proposal, rule, brief)

    async def _notify_alert(self, rule: Rule) -> None:
        q = await asyncio.to_thread(self.market.get_quote, rule.symbol)
        items = await asyncio.to_thread(self.news.fetch, rule.symbol, 3)
        price = f"${q.price:,.2f}" if q.price else "n/a"
        chg = f" ({q.change_pct:+.1f}%)" if q.change_pct is not None else ""
        lines = [f"🔔 **{rule.symbol}** — price {price}{chg}", ""]
        lines += [f"• {i.title}" for i in items] or ["No recent headlines."]
        await self.bot.send_alert(rule.chat_id, "\n".join(lines))

    def _brief_text(self, rule: Rule, brief) -> str:
        lines = [f"{c.name}: {c.actual} vs {c.expected} → {'PASS' if c.passed else 'FAIL'}"
                 for c in brief.criteria]
        lines.append(brief.summary)
        lines.append(brief.risk_note)
        return "\n".join(lines)

    async def _expire_stale_proposals(self) -> None:
        for p in self.db.pending_proposals(older_than_hours=self.settings.invoice_expiry_hours):
            try:
                await self.paypal.cancel_invoice(p.invoice_id)
            except Exception:
                log.exception("cancel stale invoice %s failed", p.invoice_id)
            self.db.update_proposal(p.id, status=ProposalStatus.CANCELLED)
            self.db.update_rule(p.rule_id, status=RuleStatus.ACTIVE)