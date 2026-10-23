#!/usr/bin/env python3
"""ONE runnable end-to-end check: rule → trigger → research → proposal → paid (webhook) → execute.

All integrations are fakes (FakeMarket / FakeNews / FakePayPal / NoopBot); no network,
no real money. Run from the repo root:

    python scripts/smoke_flow.py

Fails loudly if any link in the chain breaks.
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.integrations.alpaca import AlpacaClient  # noqa: E402
from app.schemas import ExecutionStatus, NewsItem, ProposalStatus, RuleDraft  # noqa: E402
from app.services.executor import ExecutorService  # noqa: E402
from app.services.monitor import MonitorService  # noqa: E402
from app.services.research import ResearchService  # noqa: E402
from app.services.webhook import WebhookService  # noqa: E402


class FakeMarket:
    def get_quote(self, symbol):
        return SimpleNamespace(symbol=symbol, price=150.0, change_pct=0.5)

    def get_drop_pct(self, symbol):
        return 12.5

    def get_rsi(self, symbol):
        return 42.0

    def get_detail(self, symbol):
        return SimpleNamespace(symbol=symbol, name="Apple Inc.", exchange="NASDAQ",
                               sector="Technology", price=150.0, change_pct=0.5,
                               high_52w=200.0, low_52w=120.0, pe=28.4, market_cap=2.8e12)

    def search(self, query, max_results=20):
        return []


class FakeNews:
    def fetch(self, symbol, limit=5):
        return [NewsItem(title=f"{symbol} beats estimates, shares rally", link="https://x",
                         source="Reuters", published="now")]


class FakePayPal:
    def __init__(self):
        self.invoices = []

    async def create_invoice_and_send(self, symbol, amount, description):
        self.invoices.append((symbol, amount))
        return f"INV-{len(self.invoices)}", f"https://pay.example/INV-{len(self.invoices)}"

    async def verify_webhook(self, headers, body):
        return True

    async def cancel_invoice(self, invoice_id):
        pass


class NoopBot:
    def __init__(self):
        self.proposals = []
        self.fills = []
        self.alerts = []

    async def send_proposal(self, proposal, rule, brief):
        self.proposals.append(proposal)

    async def send_alert(self, chat_id, text):
        self.alerts.append(text)

    async def notify_fill(self, proposal, execution):
        self.fills.append((proposal, execution))

    async def notify_error(self, chat_id, text):
        pass


async def main() -> int:
    tmp = tempfile.mkdtemp(prefix="stockwatch-smoke-")
    settings = Settings(_env_file=None, simulate_order=True,
                        db_path=str(Path(tmp) / "smoke.sqlite"))
    db = Database(settings.db_path)
    db.upsert_user(1, budget=1000.0)

    market, news = FakeMarket(), FakeNews()
    research = ResearchService(settings, market, news)  # no GOOGLE_API_KEY → fallback brief
    paypal, bot = FakePayPal(), NoopBot()
    monitor = MonitorService(settings, db, market, news, research, paypal, bot)
    executor = ExecutorService(settings, db, AlpacaClient(settings), market)

    # 1. rule
    rule = db.create_rule(1, RuleDraft(symbol="AAPL", condition_type="price_below",
                                       threshold=180.0, max_amount=300.0))

    # 2. monitor triggers (fake price 150 < 180) → research → proposal + invoice
    fired = await monitor.check_all()
    assert fired == [rule.id], f"rule did not fire: {fired}"
    proposals = db.list_proposals(1)
    assert len(proposals) == 1, "expected exactly one proposal"
    p = proposals[0]
    assert p.status == ProposalStatus.PROPOSED, p.status
    assert p.invoice_id and p.pay_link, "proposal missing invoice/pay link"
    assert abs(p.amount - 300.0) < 0.01, f"expected $300.00, got {p.amount}"
    assert bot.proposals == [p], "bot did not receive the proposal card"
    rule_after = db.get_rule(rule.id)
    assert rule_after.status.value == "triggered", "rule should be marked triggered"

    # 3. user pays → webhook → execute (simulated fill at fake quote 150.00)
    event = json.dumps({
        "id": "EVT-1",
        "event_type": "INVOICING.INVOICE.PAID",
        "resource": {"id": p.invoice_id,
                     "amount": {"currency_code": "USD", "value": f"{p.amount:.2f}"}},
    }).encode()
    webhook = WebhookService(settings, db, paypal, executor, bot)
    result = await webhook.process(event, {"paypal-transmission-id": "t"})
    assert result == "executed", f"webhook outcome: {result}"

    # 4. verify the fill
    executions = db.list_executions(1)
    assert len(executions) == 1, "expected exactly one execution"
    ex = executions[0]
    assert ex.status == ExecutionStatus.FILLED, ex.status
    assert ex.filled_price == 150.0
    assert abs(ex.filled_qty - 2.0) < 1e-6, f"300 / 150 should fill 2.0 shares, got {ex.filled_qty}"
    assert db.get_proposal(p.id).status == ProposalStatus.EXECUTED
    assert bot.fills and bot.fills[0][0].id == p.id, "bot did not send the fill receipt"

    print("SMOKE OK — rule → research → proposal → paid → executed")
    print(f"  rule     #{rule.id} AAPL price_below 180, max $300")
    print(f"  proposal #{p.id}   ${p.amount:.2f}  invoice {p.invoice_id}  pay link {p.pay_link}")
    print(f"  execution #{ex.id} {ex.filled_qty} AAPL @ ${ex.filled_price:.2f} "
          f"(simulated, order {ex.alpaca_order_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))