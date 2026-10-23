import json
import logging

from ..config import Settings
from ..db import Database
from ..integrations.paypal import PayPalClient
from .executor import ExecutorService

log = logging.getLogger(__name__)


class WebhookService:
    """PayPal webhook pipeline: verify signature → dedupe by event id → amount check → execute."""

    def __init__(self, settings: Settings, db: Database, paypal: PayPalClient,
                 executor: ExecutorService, bot):
        self.settings = settings
        self.db = db
        self.paypal = paypal
        self.executor = executor
        self.bot = bot

    async def process(self, body: bytes, headers: dict) -> str:
        """Returns an outcome label; never raises on bad input (webhooks get 200 + log)."""
        if not await self.paypal.verify_webhook(headers, body):
            log.warning("webhook signature verification failed")
            return "unverified"
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            log.warning("webhook body is not JSON")
            return "invalid_json"
        if not self.db.record_event_if_new(event.get("id", "")):
            return "duplicate"
        if event.get("event_type") != "INVOICING.INVOICE.PAID":
            return "ignored"

        resource = event.get("resource") or {}
        invoice_id = resource.get("id")
        amount = float((resource.get("amount") or {}).get("value") or 0)
        proposal = self.db.get_proposal_by_invoice(invoice_id) if invoice_id else None
        if proposal is None:
            log.warning("no proposal found for invoice %s", invoice_id)
            return "no_proposal"
        if abs(amount - proposal.amount) > 0.01:
            log.warning("amount mismatch: event $%.2f vs proposal $%.2f", amount, proposal.amount)
            return "amount_mismatch"

        execution = await self.executor.execute(proposal)
        await self.bot.notify_fill(proposal, execution)
        return "executed"