import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .schemas import (
    Execution,
    ExecutionStatus,
    Proposal,
    ProposalStatus,
    Rule,
    RuleDraft,
    RuleStatus,
    SearchResult,
    UserSettings,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """SQLite persistence — every row is keyed by chat_id (per-chat isolation).

    Single connection guarded by a lock: fine for a single-process demo
    (ponytail: no horizontal scaling; upgrade path = real DB + workers).
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS users(
      chat_id INTEGER PRIMARY KEY,
      budget REAL DEFAULT 1000,
      risk TEXT DEFAULT 'medium',
      notify INTEGER DEFAULT 1,
      currency TEXT DEFAULT 'USD',
      created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS rules(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      chat_id INTEGER NOT NULL,
      symbol TEXT NOT NULL,
      condition_type TEXT NOT NULL,
      threshold REAL,
      max_amount REAL,
      status TEXT DEFAULT 'active',
      cooldown_until TEXT,
      one_shot INTEGER DEFAULT 0,
      last_triggered_at TEXT,
      created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS proposals(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      chat_id INTEGER NOT NULL,
      rule_id INTEGER NOT NULL,
      symbol TEXT NOT NULL,
      amount REAL NOT NULL,
      invoice_id TEXT,
      pay_link TEXT,
      status TEXT DEFAULT 'proposed',
      brief_json TEXT,
      created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS executions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      proposal_id INTEGER NOT NULL,
      alpaca_order_id TEXT,
      symbol TEXT NOT NULL,
      notional REAL NOT NULL,
      filled_qty REAL,
      filled_price REAL,
      status TEXT DEFAULT 'pending',
      created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS search_cache(
      chat_id INTEGER NOT NULL,
      search_id TEXT NOT NULL,
      results_json TEXT NOT NULL,
      created_at TEXT,
      PRIMARY KEY (chat_id, search_id)
    );
    CREATE TABLE IF NOT EXISTS webhook_events(
      event_id TEXT PRIMARY KEY,
      processed_at TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_proposals_invoice ON proposals(invoice_id);
    CREATE INDEX IF NOT EXISTS idx_rules_status ON rules(status);
    """

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    # -- low-level -----------------------------------------------------------
    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    @staticmethod
    def _val(v: Any) -> Any:
        return v.value if isinstance(v, Enum) else v

    # -- users ---------------------------------------------------------------
    def get_user(self, chat_id: int) -> UserSettings | None:
        rows = self._query("SELECT * FROM users WHERE chat_id=?", (chat_id,))
        return UserSettings(**dict(rows[0])) if rows else None

    def upsert_user(self, chat_id: int, **fields: Any) -> UserSettings:
        if self.get_user(chat_id) is None:
            self._execute(
                "INSERT INTO users(chat_id, budget, risk, notify, currency, created_at) VALUES (?,?,?,?,?,?)",
                (chat_id, fields.get("budget", 1000.0), fields.get("risk", "medium"),
                 int(fields.get("notify", True)), fields.get("currency", "USD"), now_iso()),
            )
        elif fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self._execute(f"UPDATE users SET {cols} WHERE chat_id=?", (*fields.values(), chat_id))
        return self.get_user(chat_id)

    # -- rules ---------------------------------------------------------------
    def create_rule(self, chat_id: int, draft: RuleDraft) -> Rule:
        cur = self._execute(
            "INSERT INTO rules(chat_id, symbol, condition_type, threshold, max_amount, created_at) VALUES (?,?,?,?,?,?)",
            (chat_id, draft.symbol, self._val(draft.condition_type), draft.threshold, draft.max_amount, now_iso()),
        )
        return self.get_rule(cur.lastrowid)

    def get_rule(self, rule_id: int) -> Rule | None:
        rows = self._query("SELECT * FROM rules WHERE id=?", (rule_id,))
        return Rule(**dict(rows[0])) if rows else None

    def update_rule(self, rule_id: int, **fields: Any) -> Rule | None:
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self._execute(f"UPDATE rules SET {cols} WHERE id=?", (*[self._val(v) for v in fields.values()], rule_id))
        return self.get_rule(rule_id)

    def list_rules(self, chat_id: int) -> list[Rule]:
        rows = self._query("SELECT * FROM rules WHERE chat_id=? ORDER BY id", (chat_id,))
        return [Rule(**dict(r)) for r in rows]

    def active_rules(self) -> list[Rule]:
        rows = self._query("SELECT * FROM rules WHERE status=?", (RuleStatus.ACTIVE.value,))
        return [Rule(**dict(r)) for r in rows]

    def delete_rule(self, rule_id: int) -> None:
        self._execute("DELETE FROM rules WHERE id=?", (rule_id,))

    # -- proposals -----------------------------------------------------------
    def create_proposal(self, chat_id: int, rule_id: int, symbol: str, amount: float,
                        invoice_id: str | None, pay_link: str | None, brief_json: str) -> Proposal:
        cur = self._execute(
            "INSERT INTO proposals(chat_id, rule_id, symbol, amount, invoice_id, pay_link, brief_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (chat_id, rule_id, symbol, amount, invoice_id, pay_link, brief_json, now_iso()),
        )
        return self.get_proposal(cur.lastrowid)

    def get_proposal(self, proposal_id: int) -> Proposal | None:
        rows = self._query("SELECT * FROM proposals WHERE id=?", (proposal_id,))
        return Proposal(**dict(rows[0])) if rows else None

    def get_proposal_by_invoice(self, invoice_id: str) -> Proposal | None:
        rows = self._query("SELECT * FROM proposals WHERE invoice_id=? LIMIT 1", (invoice_id,))
        return Proposal(**dict(rows[0])) if rows else None

    def update_proposal(self, proposal_id: int, **fields: Any) -> Proposal | None:
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self._execute(f"UPDATE proposals SET {cols} WHERE id=?", (*[self._val(v) for v in fields.values()], proposal_id))
        return self.get_proposal(proposal_id)

    def list_proposals(self, chat_id: int) -> list[Proposal]:
        rows = self._query("SELECT * FROM proposals WHERE chat_id=? ORDER BY id", (chat_id,))
        return [Proposal(**dict(r)) for r in rows]

    def pending_proposals(self, older_than_hours: float) -> list[Proposal]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=older_than_hours)).isoformat(timespec="seconds")
        rows = self._query("SELECT * FROM proposals WHERE status=? AND created_at < ?",
                           (ProposalStatus.PROPOSED.value, cutoff))
        return [Proposal(**dict(r)) for r in rows]

    # -- executions ----------------------------------------------------------
    def create_execution(self, proposal_id: int, symbol: str, notional: float,
                         order_id: str | None = None, qty: float | None = None,
                         price: float | None = None,
                         status: ExecutionStatus = ExecutionStatus.PENDING) -> Execution:
        cur = self._execute(
            "INSERT INTO executions(proposal_id, alpaca_order_id, symbol, notional, filled_qty, filled_price, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (proposal_id, order_id, symbol, notional, qty, price, self._val(status), now_iso()),
        )
        rows = self._query("SELECT * FROM executions WHERE id=?", (cur.lastrowid,))
        return Execution(**dict(rows[0]))

    def list_executions(self, chat_id: int) -> list[Execution]:
        rows = self._query(
            "SELECT e.* FROM executions e JOIN proposals p ON p.id = e.proposal_id "
            "WHERE p.chat_id=? ORDER BY e.id DESC", (chat_id,))
        return [Execution(**dict(r)) for r in rows]

    # -- search cache (callback payloads stay <= 64 bytes) -------------------
    def save_search(self, chat_id: int, search_id: str, results: list[dict]) -> None:
        self._execute(
            "INSERT OR REPLACE INTO search_cache(chat_id, search_id, results_json, created_at) VALUES (?,?,?,?)",
            (chat_id, search_id, json.dumps(results), now_iso()),
        )

    def get_search(self, chat_id: int, search_id: str) -> list[SearchResult] | None:
        rows = self._query("SELECT results_json FROM search_cache WHERE chat_id=? AND search_id=?",
                           (chat_id, search_id))
        if not rows:
            return None
        return [SearchResult(**r) for r in json.loads(rows[0]["results_json"])]

    # -- webhook dedupe ------------------------------------------------------
    def record_event_if_new(self, event_id: str) -> bool:
        if not event_id:
            return False
        with self._lock:
            cur = self._conn.execute("INSERT OR IGNORE INTO webhook_events(event_id) VALUES (?)", (event_id,))
            self._conn.commit()
            return cur.rowcount > 0