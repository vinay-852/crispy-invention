from enum import Enum

from pydantic import BaseModel


class ConditionType(str, Enum):
    PRICE_BELOW = "price_below"
    DROP_PCT = "drop_pct"
    RSI_BELOW = "rsi_below"
    SENTIMENT_POSITIVE = "sentiment_positive"
    NOTIFY = "notify"


class RuleStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    TRIGGERED = "triggered"


class ProposalStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    CANCELLED = "cancelled"


class ExecutionStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    FAILED = "failed"


class UserSettings(BaseModel):
    chat_id: int
    budget: float = 1000.0
    risk: str = "medium"
    notify: bool = True
    currency: str = "USD"


class Rule(BaseModel):
    id: int
    chat_id: int
    symbol: str
    condition_type: ConditionType
    threshold: float | None = None
    max_amount: float | None = None
    status: RuleStatus = RuleStatus.ACTIVE
    cooldown_until: str | None = None
    one_shot: bool = False
    last_triggered_at: str | None = None
    created_at: str | None = None


class RuleDraft(BaseModel):
    symbol: str
    condition_type: ConditionType
    threshold: float | None = None
    max_amount: float | None = None


class Proposal(BaseModel):
    id: int
    chat_id: int
    rule_id: int
    symbol: str
    amount: float
    invoice_id: str | None = None
    pay_link: str | None = None
    status: ProposalStatus = ProposalStatus.PROPOSED
    brief_json: str | None = None
    created_at: str | None = None


class Execution(BaseModel):
    id: int
    proposal_id: int
    alpaca_order_id: str | None = None
    symbol: str
    notional: float
    filled_qty: float | None = None
    filled_price: float | None = None
    status: ExecutionStatus = ExecutionStatus.PENDING
    created_at: str | None = None


class SearchResult(BaseModel):
    symbol: str
    name: str = ""
    exchange: str = ""
    price: float | None = None
    change_pct: float | None = None
    score: float = 0.0


class NewsItem(BaseModel):
    title: str
    link: str = ""
    source: str = ""
    published: str = ""


class CriterionScore(BaseModel):
    name: str
    expected: str
    actual: str
    passed: bool


class ResearchBrief(BaseModel):
    symbol: str
    criteria: list[CriterionScore] = []
    summary: str = ""
    recommendation_amount: float | None = None
    risk_note: str = ""
    sources: list[str] = []