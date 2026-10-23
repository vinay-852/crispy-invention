from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..config import Settings
from ..schemas import Execution, Proposal, ResearchBrief, Rule, RuleDraft

CONDITION_TEXT = {
    "price_below": "price below",
    "drop_pct": "dropped more than",
    "rsi_below": "RSI(14) below",
    "sentiment_positive": "news sentiment positive",
    "notify": "just notify",
}


class CardBuilder:
    """Message + inline-keyboard builders for the Telegram UI (spec §10)."""

    @staticmethod
    def start() -> tuple[str, InlineKeyboardMarkup]:
        text = (
            "👋 Welcome to **StockWatch AI** — your AI investment agent.\n\n"
            "Tell me a condition, e.g. *buy AAPL when it drops below $180*, and I'll research "
            "the market, ask you for funds via PayPal, and buy on your approval.\n\n"
            "Everything runs on **sandbox / paper rails** — no real money.\n\n"
            "Try `/watch apple under 180 max 250`, or search the market:"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Watch a stock", callback_data="search")],
            [InlineKeyboardButton("💰 Set budget", callback_data="budget")],
            [InlineKeyboardButton("📋 How it works", callback_data="help")],
        ])
        return text, kb

    @staticmethod
    def help() -> str:
        return (
            "📋 **How it works**\n\n"
            "1. Set a rule: *buy AAPL if price < $180, max $300*\n"
            "2. I watch the market every few minutes\n"
            "3. When your condition fires, I research (news + sentiment) and send an investment brief\n"
            "4. You approve by **paying a PayPal invoice** (sandbox), ✏️ modify, or ❌ reject\n"
            "5. On payment I place the **paper trade** on Alpaca and send a receipt\n\n"
            "Commands:\n"
            "/search · /watch · /rules · /remove <id> · /pause <id> · /resume <id>\n"
            "/budget <amount> · /risk <low|medium|high> · /notify on|off\n"
            "/history · /status · /cancel"
        )

    @staticmethod
    def detail(detail, rsi: float | None, sentiment: float, headlines: list[str]) -> tuple[str, InlineKeyboardMarkup]:
        arrow = "▲" if (detail.change_pct or 0) >= 0 else "▼"
        senti_label = ("positive" if sentiment > 0 else "neutral" if sentiment == 0 else "negative")
        price = f"**${detail.price:,.2f}**" if detail.price is not None else "**n/a**"
        change = f" ({arrow}{abs(detail.change_pct):.1f}%)" if detail.change_pct is not None else ""
        range_52w = (f"${detail.low_52w:,.2f}–${detail.high_52w:,.2f}"
                     if detail.low_52w is not None and detail.high_52w is not None else "n/a")
        cap = f"${detail.market_cap / 1e9:,.1f}B" if detail.market_cap else "—"
        text = (
            f"📈 **{detail.symbol} — {detail.name}**\n"
            f"{detail.exchange} · {detail.sector or '—'}\n\n"
            f"Price {price}{change} · 52w {range_52w}\n"
            f"P/E {detail.pe or '—'} · Market cap {cap} · "
            f"RSI(14) {rsi if rsi is not None else '—'}\n"
            f"News sentiment: **{senti_label}** ({len(headlines)} articles)\n\n"
            "What should I do?"
        )
        sym = detail.symbol
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Buy if price below…", callback_data=f"cond:{sym}:price_below")],
            [InlineKeyboardButton("📉 Buy if dropped > X%", callback_data=f"cond:{sym}:drop_pct")],
            [InlineKeyboardButton("📊 Buy if RSI below…", callback_data=f"cond:{sym}:rsi_below")],
            [InlineKeyboardButton("📰 Sentiment positive", callback_data=f"cond:{sym}:sentiment_positive")],
            [InlineKeyboardButton("🔔 Just notify", callback_data=f"cond:{sym}:notify")],
        ])
        return text, kb

    @staticmethod
    def confirm_rule(draft: RuleDraft, budget: float) -> tuple[str, InlineKeyboardMarkup]:
        cond = {
            "price_below": (f"price below **${draft.threshold:,.2f}**"
                            if draft.threshold is not None else "price below a target"),
            "drop_pct": (f"dropped more than **{draft.threshold:,.0f}%** from 52w high"
                         if draft.threshold is not None else "dropped more than X%"),
            "rsi_below": (f"RSI(14) below **{draft.threshold:,.1f}**"
                          if draft.threshold is not None else "RSI(14) below a level"),
            "sentiment_positive": "news sentiment **positive**",
            "notify": "just **notify** me",
        }[draft.condition_type.value]
        amount = f"**${draft.max_amount:,.2f}** max per trade" if draft.max_amount else "notify only — no purchase"
        warn = (f"\n⚠️ Amount exceeds your budget (${budget:,.0f})."
                if draft.max_amount and budget and draft.max_amount > budget else "")
        text = f"📋 Confirm your rule:\n\nWatch **{draft.symbol}** — {cond}\n{amount}{warn}\n\nSave?"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Save rule", callback_data="draft:ok"),
             InlineKeyboardButton("❌ Cancel", callback_data="draft:no")],
        ])
        return text, kb

    @staticmethod
    def rule_saved(rule: Rule) -> str:
        return (f"✅ Rule #{rule.id} saved: **{rule.symbol}** — {CONDITION_TEXT[rule.condition_type.value]}.\n"
                "I'll check it every few minutes and propose a trade when it fires. See /rules.")

    @staticmethod
    def proposal(proposal: Proposal, rule: Rule, brief: ResearchBrief) -> tuple[str, InlineKeyboardMarkup]:
        lines = [f"📈 **INVESTMENT PROPOSAL — {proposal.symbol}**",
                 f"Rule #{rule.id}: {CONDITION_TEXT[rule.condition_type.value]}", ""]
        for c in brief.criteria:
            mark = "✅" if c.passed else "❌"
            lines.append(f"{mark} {c.name}: {c.actual} vs {c.expected}")
        lines += ["", brief.summary, f"*Risk:* {brief.risk_note}", "",
                  f"Recommend: **${proposal.amount:,.2f}**"]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💳 Pay ${proposal.amount:,.2f} via PayPal",
                                  callback_data=f"proposal:{proposal.id}:pay"),
             InlineKeyboardButton("✏️ Modify", callback_data=f"proposal:{proposal.id}:modify")],
            [InlineKeyboardButton("❌ Reject", callback_data=f"proposal:{proposal.id}:reject")],
        ])
        return "\n".join(lines), kb

    @staticmethod
    def fill_receipt(proposal: Proposal, execution: Execution) -> str:
        return (
            f"✅ **FILLED — {execution.symbol}**\n"
            f"Paid: **${proposal.amount:,.2f}** via PayPal (sandbox)\n"
            f"Filled: {execution.filled_qty} {execution.symbol} @ **${execution.filled_price:,.2f}**\n"
            f"Order: {execution.alpaca_order_id or '—'} · Paper trade"
        )

    @staticmethod
    def rules_list(rules: list[Rule]) -> str:
        if not rules:
            return "No rules yet. Use /search to build one."
        status_icon = {"active": "🟢", "paused": "⏸️", "triggered": "⏳"}
        lines = ["📋 **Your rules**"]
        for r in rules:
            cond = CONDITION_TEXT[r.condition_type.value]
            extra = f" {r.threshold:,.2f}" if r.threshold is not None else ""
            max_amt = f" · max ${r.max_amount:,.0f}" if r.max_amount else ""
            lines.append(f"{status_icon[r.status.value]} #{r.id} **{r.symbol}** — {cond}{extra}{max_amt}")
        lines.append("\n/remove <id> · /pause <id> · /resume <id>")
        return "\n".join(lines)

    @staticmethod
    def history_list(executions: list[Execution]) -> str:
        if not executions:
            return "No executions yet. When a proposal is paid, the receipt lands here."
        lines = ["🧾 **Execution history**"]
        for e in executions:
            price = f"${e.filled_price:,.2f}" if e.filled_price else "—"
            qty = f"{e.filled_qty}" if e.filled_qty else "—"
            lines.append(f"• **{e.symbol}**: {qty} @ {price} — ${e.notional:,.2f} ({e.status.value})")
        return "\n".join(lines)

    @staticmethod
    def status(active_count: int, last_check: str | None, next_check: str | None,
               settings: Settings) -> str:
        mode = "SIMULATED fills" if settings.simulate_order else "Alpaca paper"
        return (
            "🩺 **Status**\n"
            f"Active rules: {active_count}\n"
            f"Mode: {mode}\n"
            f"Last check: {last_check or 'never'}\n"
            f"Next check: {next_check or '—'}\n"
            f"Poll interval: {settings.poll_interval_minutes} min"
        )