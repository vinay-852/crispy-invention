import asyncio
import logging
import uuid

from ..config import Settings
from ..db import Database
from ..integrations.alpaca import AlpacaClient
from ..providers.market import MarketData
from ..schemas import Execution, ExecutionStatus, Proposal, ProposalStatus, RuleStatus

log = logging.getLogger(__name__)


class ExecutorService:
    """Payment-confirmed execution: Alpaca paper order, or SIMULATE_ORDER fill from a live quote."""

    def __init__(self, settings: Settings, db: Database, alpaca: AlpacaClient, market: MarketData):
        self.settings = settings
        self.db = db
        self.alpaca = alpaca
        self.market = market

    async def execute(self, proposal: Proposal) -> Execution:
        if proposal.status != ProposalStatus.PROPOSED:
            raise RuntimeError(f"Proposal {proposal.id} is {proposal.status.value}, not proposed")
        self.db.update_proposal(proposal.id, status=ProposalStatus.APPROVED)
        try:
            execution = await self._place_order(proposal)
        except Exception as e:
            log.exception("order for proposal %s failed", proposal.id)
            self.db.create_execution(proposal.id, proposal.symbol, proposal.amount,
                                     status=ExecutionStatus.FAILED)
            raise RuntimeError(f"Order failed: {e}") from e

        if execution.status == ExecutionStatus.FILLED:
            self.db.update_proposal(proposal.id, status=ProposalStatus.EXECUTED)
            rule = self.db.get_rule(proposal.rule_id)
            if rule:
                if rule.one_shot:
                    self.db.update_rule(rule.id, status=RuleStatus.PAUSED)
                else:
                    self.db.update_rule(rule.id, status=RuleStatus.ACTIVE)
        return execution

    async def _place_order(self, proposal: Proposal) -> Execution:
        if self.settings.simulate_order:
            quote = await asyncio.to_thread(self.market.get_quote, proposal.symbol)
            price = quote.price
            if not price:
                raise RuntimeError("No live quote available for simulated fill")
            qty = round(proposal.amount / price, 4)
            return self.db.create_execution(
                proposal.id, proposal.symbol, proposal.amount,
                order_id=f"SIM-{uuid.uuid4().hex[:12].upper()}",
                qty=qty, price=price, status=ExecutionStatus.FILLED)

        order = await asyncio.to_thread(self.alpaca.submit_market_order, proposal.symbol, proposal.amount)
        qty = float(order.filled_qty or 0)
        price = float(order.filled_avg_price or 0)
        return self.db.create_execution(
            proposal.id, proposal.symbol, proposal.amount,
            order_id=order.id, qty=qty, price=price, status=ExecutionStatus.FILLED)