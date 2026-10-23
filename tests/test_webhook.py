import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.schemas import Execution, ExecutionStatus, ProposalStatus, RuleDraft
from app.services.webhook import WebhookService


class FakePayPal:
    def __init__(self, verified: bool = True):
        self.verified = verified

    async def verify_webhook(self, headers: dict, body: bytes) -> bool:
        return self.verified

    async def cancel_invoice(self, invoice_id: str | None) -> None:
        pass


class FakeExecutor:
    def __init__(self, db=None):
        self.calls: list[int] = []
        self.db = db

    async def execute(self, proposal):
        self.calls.append(proposal.id)
        if self.db:  # mimic the real ExecutorService's DB effect
            self.db.update_proposal(proposal.id, status=ProposalStatus.EXECUTED)
        return Execution(id=1, proposal_id=proposal.id, symbol=proposal.symbol,
                         notional=proposal.amount, filled_qty=1.0, filled_price=100.0,
                         status=ExecutionStatus.FILLED)


class FakeBot:
    def __init__(self):
        self.fills: list[tuple[int, int]] = []

    async def notify_fill(self, proposal, execution) -> None:
        self.fills.append((proposal.id, execution.id))


def make_event(invoice_id: str, amount: float, event_id: str = "EVT-1") -> bytes:
    return json.dumps({
        "id": event_id,
        "event_type": "INVOICING.INVOICE.PAID",
        "resource": {"id": invoice_id, "amount": {"currency_code": "USD", "value": f"{amount:.2f}"}},
    }).encode()


@pytest.fixture
def ctx(tmp_path):
    db = Database(str(tmp_path / "test.sqlite"))
    db.upsert_user(1, budget=1000.0)
    rule = db.create_rule(1, RuleDraft(symbol="AAPL", condition_type="price_below",
                                       threshold=180.0, max_amount=300.0))
    proposal = db.create_proposal(1, rule.id, "AAPL", 300.0, "INV-1",
                                  "https://pay.example/INV-1", "{}")
    return db, proposal


def test_paid_event_executes(ctx):
    db, proposal = ctx
    executor, bot = FakeExecutor(db), FakeBot()
    svc = WebhookService(Settings(_env_file=None), db, FakePayPal(), executor, bot)
    result = asyncio.run(svc.process(make_event("INV-1", 300.0), {"paypal-transmission-id": "t"}))
    assert result == "executed"
    assert executor.calls == [proposal.id]
    assert bot.fills == [(proposal.id, 1)]
    assert db.get_proposal(proposal.id).status == ProposalStatus.EXECUTED


def test_duplicate_event_is_deduplicated(ctx):
    db, proposal = ctx
    executor, bot = FakeExecutor(db), FakeBot()
    svc = WebhookService(Settings(_env_file=None), db, FakePayPal(), executor, bot)
    event = make_event("INV-1", 300.0)
    assert asyncio.run(svc.process(event, {})) == "executed"
    assert asyncio.run(svc.process(event, {})) == "duplicate"
    assert executor.calls == [proposal.id]  # executed exactly once


def test_amount_mismatch_blocks_execution(ctx):
    db, proposal = ctx
    executor, bot = FakeExecutor(db), FakeBot()
    svc = WebhookService(Settings(_env_file=None), db, FakePayPal(), executor, bot)
    result = asyncio.run(svc.process(make_event("INV-1", 299.0), {}))
    assert result == "amount_mismatch"
    assert executor.calls == []
    assert db.get_proposal(proposal.id).status == ProposalStatus.PROPOSED


def test_unverified_signature_rejected(ctx):
    db, proposal = ctx
    executor, bot = FakeExecutor(db), FakeBot()
    svc = WebhookService(Settings(_env_file=None), db, FakePayPal(verified=False), executor, bot)
    assert asyncio.run(svc.process(make_event("INV-1", 300.0), {})) == "unverified"
    assert executor.calls == []
    assert db.get_proposal(proposal.id).status == ProposalStatus.PROPOSED


def test_unknown_invoice_ignored(ctx):
    db, proposal = ctx
    executor, bot = FakeExecutor(db), FakeBot()
    svc = WebhookService(Settings(_env_file=None), db, FakePayPal(), executor, bot)
    assert asyncio.run(svc.process(make_event("INV-NOPE", 300.0), {})) == "no_proposal"
    assert executor.calls == []


def test_http_route_returns_200_and_processes_in_background(tmp_path):
    settings = Settings(_env_file=None, db_path=str(tmp_path / "http.sqlite"))
    db = Database(settings.db_path)
    db.upsert_user(1)
    rule = db.create_rule(1, RuleDraft(symbol="AAPL", condition_type="price_below",
                                       threshold=180.0, max_amount=300.0))
    proposal = db.create_proposal(1, rule.id, "AAPL", 300.0, "INV-HTTP",
                                  "https://pay.example/INV-HTTP", "{}")
    executor, bot = FakeExecutor(db), FakeBot()
    app = create_app(settings=settings, db=db, paypal=FakePayPal(), executor=executor, bot=bot)

    with TestClient(app) as client:
        resp = client.post("/webhooks/paypal", content=make_event("INV-HTTP", 300.0),
                           headers={"PayPal-Transmission-Id": "t"})
        assert resp.status_code == 200

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not executor.calls:
            time.sleep(0.05)
        assert executor.calls == [proposal.id]
        assert db.get_proposal(proposal.id).status == ProposalStatus.EXECUTED