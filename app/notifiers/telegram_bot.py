import asyncio
import logging
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ..config import Settings
from ..db import Database
from ..integrations.paypal import PayPalClient
from ..providers.market import MarketData
from ..providers.news import NewsProvider
from ..schemas import ConditionType, ProposalStatus, Rule, RuleDraft, RuleStatus
from ..services.parser import RuleModifier, WatchCommandParser
from ..services.research import ResearchService
from .cards import CardBuilder

log = logging.getLogger(__name__)

# Wizard states (spec §8)
SEARCH_QUERY, RESULTS, DETAIL, THRESHOLD, AMOUNT, CONFIRM, MODIFY = range(7)

PAGE_SIZE = 6
MAX_AMOUNT = 1_000_000


class TelegramBot:
    """python-telegram-bot app: commands, search wizard, proposal actions, JobQueue tick.

    Telegram is UI-only — all logic lives in the services.
    """

    def __init__(self, settings: Settings, db: Database, market: MarketData, news: NewsProvider,
                 research: ResearchService, paypal: PayPalClient, cards: CardBuilder):
        self.settings = settings
        self.db = db
        self.market = market
        self.news = news
        self.research = research
        self.paypal = paypal
        self.cards = cards
        self.watch_parser = WatchCommandParser(model=settings.adk_model)
        self.rule_modifier = RuleModifier(model=settings.adk_model)
        self.monitor = None
        self.application = (Application.builder().token(settings.telegram_bot_token).build()
                            if settings.telegram_bot_token else None)
        self._started = False
        if self.application:
            self._register_handlers()

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        if not self.application or self._started:
            return
        self._started = True
        self.application.job_queue.run_repeating(
            self._tick, interval=self.settings.poll_interval_minutes * 60, first=15)
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling(drop_pending_updates=True)

    async def stop(self) -> None:
        if not self.application or not self._started:
            return
        await self.application.updater.stop()
        await self.application.stop()
        await self.application.shutdown()

    async def _tick(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.monitor:
            try:
                await self.monitor.check_all()
            except Exception:
                log.exception("monitor tick failed")

    # -- guards & send helpers ----------------------------------------------
    def _allowed(self, chat_id: int | None) -> bool:
        return not self.settings.allowed_chat_ids or chat_id in self.settings.allowed_chat_ids

    def _guard(self, fn):
        """Allowlist check; blocked chats are silently ignored."""
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            chat_id = update.effective_chat.id if update.effective_chat else None
            if chat_id is None or not self._allowed(chat_id):
                log.warning("blocked chat %s", chat_id)
                return None
            return await fn(update, context)
        return wrapper

    async def _send(self, chat_id: int, text: str, **kwargs):
        for attempt in (1, 2):
            try:
                return await self.application.bot.send_message(chat_id, text, **kwargs)
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramError:
                if kwargs.get("parse_mode") and attempt == 1:
                    kwargs = {**kwargs, "parse_mode": None}
                    continue
                raise
        return None

    async def _edit(self, query, text: str | None, **kwargs):
        for attempt in (1, 2):
            try:
                if text is not None:
                    return await query.edit_message_text(text, **kwargs)
                return await query.edit_message_reply_markup(**kwargs)
            except TelegramError:
                if kwargs.get("parse_mode") and attempt == 1:
                    kwargs = {**kwargs, "parse_mode": None}
                    continue
                raise
        return None

    @staticmethod
    def _parse_number(text: str) -> float | None:
        try:
            return float(text.replace(",", "").replace("$", "").strip())
        except ValueError:
            return None

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception("bot handler error: %s", context.error)

    # -- handler registration ------------------------------------------------
    def _register_handlers(self) -> None:
        app = self.application
        app.add_error_handler(self._on_error)

        wizard = ConversationHandler(
            entry_points=[
                CommandHandler("search", self._guard(self.cmd_search)),
                CommandHandler("watch", self._guard(self.cmd_watch)),
                CallbackQueryHandler(self._guard(self.btn_search), pattern="^search$"),
            ],
            states={
                SEARCH_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._guard(self.on_search_query))],
                RESULTS: [
                    CallbackQueryHandler(self._guard(self.on_page), pattern="^page:"),
                    CallbackQueryHandler(self._guard(self.on_pick), pattern="^pick:"),
                ],
                DETAIL: [CallbackQueryHandler(self._guard(self.on_condition), pattern="^cond:")],
                THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._guard(self.on_threshold))],
                AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._guard(self.on_amount))],
                CONFIRM: [CallbackQueryHandler(self._guard(self.on_confirm), pattern="^draft:")],
            },
            fallbacks=[
                CommandHandler("cancel", self._guard(self.cmd_cancel)),
                CallbackQueryHandler(self._guard(self.btn_cancel_wizard), pattern="^cancel$"),
            ],
            per_message=False,
        )

        modify = ConversationHandler(
            entry_points=[CallbackQueryHandler(self._guard(self.on_modify_start), pattern="^proposal:.*:modify$")],
            states={MODIFY: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._guard(self.on_modify_text))]},
            fallbacks=[CommandHandler("cancel", self._guard(self.cmd_cancel))],
            per_message=False,
        )

        app.add_handler(modify)
        app.add_handler(wizard)
        app.add_handler(CallbackQueryHandler(self._guard(self.on_proposal_action), pattern="^proposal:"))
        app.add_handler(CallbackQueryHandler(self._guard(self.on_menu), pattern="^(budget|help)$"))
        for cmd, fn in [
            ("start", self.cmd_start), ("help", self.cmd_help),
            ("rules", self.cmd_rules), ("remove", self.cmd_remove),
            ("pause", self.cmd_pause), ("resume", self.cmd_resume),
            ("budget", self.cmd_budget), ("risk", self.cmd_risk),
            ("notify", self.cmd_notify), ("history", self.cmd_history),
            ("status", self.cmd_status),
        ]:
            app.add_handler(CommandHandler(cmd, self._guard(fn)))

    # -- basic commands ------------------------------------------------------
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.db.upsert_user(update.effective_chat.id)
        text, kb = self.cards.start()
        await self._send(update.effective_chat.id, text, reply_markup=kb, parse_mode="Markdown")

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._send(update.effective_chat.id, self.cards.help(), parse_mode="Markdown")

    async def cmd_rules(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        rules = self.db.list_rules(update.effective_chat.id)
        await self._send(update.effective_chat.id, self.cards.rules_list(rules), parse_mode="Markdown")

    async def _own_rule(self, update: Update, parts: list[str]) -> Rule | None:
        if len(parts) != 2 or not parts[1].isdigit():
            await self._send(update.effective_chat.id, "Usage: /remove <id> or /pause <id> — see /rules")
            return None
        rule = self.db.get_rule(int(parts[1]))
        if rule is None or rule.chat_id != update.effective_chat.id:
            await self._send(update.effective_chat.id, "Rule not found.")
            return None
        return rule

    async def cmd_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        rule = await self._own_rule(update, update.message.text.split())
        if not rule:
            return
        self.db.delete_rule(rule.id)
        await self._send(update.effective_chat.id, f"Removed rule #{rule.id} ({rule.symbol}).")

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        rule = await self._own_rule(update, update.message.text.split())
        if not rule:
            return
        self.db.update_rule(rule.id, status=RuleStatus.PAUSED)
        await self._send(update.effective_chat.id, f"⏸️ Paused rule #{rule.id} ({rule.symbol}).")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        rule = await self._own_rule(update, update.message.text.split())
        if not rule:
            return
        self.db.update_rule(rule.id, status=RuleStatus.ACTIVE)
        await self._send(update.effective_chat.id, f"▶️ Resumed rule #{rule.id} ({rule.symbol}).")

    async def cmd_budget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        parts = update.message.text.split()
        val = self._parse_number(parts[1]) if len(parts) == 2 else None
        if not val or val <= 0:
            await self._send(update.effective_chat.id, "Usage: /budget <amount> e.g. /budget 1000")
            return
        self.db.upsert_user(update.effective_chat.id, budget=val)
        await self._send(update.effective_chat.id, f"💰 Budget set to ${val:,.2f}.")

    async def cmd_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        parts = update.message.text.split()
        val = parts[1].lower() if len(parts) > 1 else ""
        if val not in ("low", "medium", "high"):
            await self._send(update.effective_chat.id, "Usage: /risk <low|medium|high>")
            return
        self.db.upsert_user(update.effective_chat.id, risk=val)
        await self._send(update.effective_chat.id, f"🎚️ Risk profile set to **{val}**.", parse_mode="Markdown")

    async def cmd_notify(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        parts = update.message.text.split()
        val = parts[1].lower() if len(parts) > 1 else ""
        if val not in ("on", "off"):
            await self._send(update.effective_chat.id, "Usage: /notify on|off")
            return
        self.db.upsert_user(update.effective_chat.id, notify=val == "on")
        await self._send(update.effective_chat.id, f"🔔 Notifications **{val}**.", parse_mode="Markdown")

    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        executions = self.db.list_executions(update.effective_chat.id)
        await self._send(update.effective_chat.id, self.cards.history_list(executions), parse_mode="Markdown")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        active = len([r for r in self.db.list_rules(update.effective_chat.id)
                      if r.status == RuleStatus.ACTIVE])
        last = self.monitor.last_check_at if self.monitor else None
        nxt = self.monitor.next_check_at if self.monitor else None
        await self._send(update.effective_chat.id,
                         self.cards.status(active, last, nxt, self.settings), parse_mode="Markdown")

    async def on_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        if query.data == "budget":
            await self._edit(query, "💰 Set your trading budget:\n`/budget 1000` — max per trade\n"
                                    "`/risk medium` — low | medium | high", parse_mode="Markdown")
        else:
            await self._edit(query, self.cards.help(), parse_mode="Markdown")

    # -- search wizard -------------------------------------------------------
    async def cmd_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await self._send(update.effective_chat.id,
                         "🔎 What would you like to watch? Name, ticker or keyword "
                         "(e.g. \"apple\" or \"electric vehicles\").")
        return SEARCH_QUERY

    async def btn_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        await self._edit(query, "🔎 What would you like to watch? Name, ticker or keyword.")
        return SEARCH_QUERY

    async def on_search_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query_text = update.message.text.strip()
        msg = await self._send(update.effective_chat.id, "🔎 Searching…")
        try:
            results = await asyncio.to_thread(self.market.search, query_text, 20)
        except Exception as e:
            log.exception("search failed")
            await msg.edit_text(f"⚠️ Search failed: {e}. Try a different query or /cancel.")
            return SEARCH_QUERY
        if not results:
            await msg.edit_text("No equities found. Try a different query.")
            return SEARCH_QUERY
        search_id = uuid.uuid4().hex
        self.db.save_search(update.effective_chat.id, search_id, [r.model_dump() for r in results])
        context.user_data["search_id"] = search_id
        context.user_data["page"] = 0
        text, kb = self._results_message(update.effective_chat.id, search_id, 0, results)
        await msg.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        return RESULTS

    def _results_message(self, chat_id: int, search_id: str, page: int, results: list[RuleDraft]):
        total = len(results)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(0, page), pages - 1)
        start, end = page * PAGE_SIZE, min((page + 1) * PAGE_SIZE, total)
        lines = [f"🔍 Results {start + 1}–{end} of {total}:"]
        buttons = []
        for i, r in enumerate(results[start:end]):
            price = f"${r.price:,.2f}" if r.price else "—"
            lines.append(f"• **{r.symbol}** {r.name[:34]}")
            buttons.append([InlineKeyboardButton(f"{r.symbol} {r.name[:18]} {price}",
                                                 callback_data=f"pick:{search_id}:{start + i}")])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"page:{search_id}:prev"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"page:{search_id}:next"))
        nav.append(InlineKeyboardButton("❌ Cancel", callback_data="cancel"))
        buttons.append(nav)
        return "\n".join(lines), InlineKeyboardMarkup(buttons)

    async def on_page(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        _, search_id, direction = query.data.split(":")
        results = self.db.get_search(update.effective_chat.id, search_id)
        if not results:
            await self._edit(query, "Search expired. Run /search again.")
            return ConversationHandler.END
        page = int(context.user_data.get("page", 0))
        page = max(0, min(page + (1 if direction == "next" else -1),
                          (len(results) - 1) // PAGE_SIZE))
        context.user_data["page"] = page
        text, kb = self._results_message(update.effective_chat.id, search_id, page, results)
        await self._edit(query, text, reply_markup=kb, parse_mode="Markdown")
        return RESULTS

    async def on_pick(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        _, search_id, idx = query.data.split(":")
        results = self.db.get_search(update.effective_chat.id, search_id) or []
        if not results or int(idx) >= len(results):
            await self._edit(query, "Search expired. Run /search again.")
            return ConversationHandler.END
        result = results[int(idx)]
        context.user_data["symbol"] = result.symbol
        await self._edit(query, "⏳ Loading…")
        detail = await asyncio.to_thread(self.market.get_detail, result.symbol)
        rsi = await asyncio.to_thread(self.market.get_rsi, result.symbol)
        items = await asyncio.to_thread(self.news.fetch, result.symbol, 3)
        senti, headlines = self.research.sentiment.analyze(items)
        text, kb = self.cards.detail(detail, rsi, senti, headlines)
        await self._edit(query, text, reply_markup=kb, parse_mode="Markdown")
        return DETAIL

    async def on_condition(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        _, symbol, ctype = query.data.split(":", 2)
        context.user_data["symbol"] = symbol
        context.user_data["ctype"] = ctype
        if ctype == "notify":
            context.user_data["threshold"] = None
            context.user_data["amount"] = None
            return await self._confirm(query, context)
        if ctype == "sentiment_positive":
            context.user_data["threshold"] = None
            return await self._ask_amount(query, context)
        prompt = {
            "price_below": f"📉 Buy {symbol} if the price drops below **$?** (e.g. 180)",
            "drop_pct": f"📉 Buy {symbol} if it drops more than **?%** from its 52-week high (e.g. 10)",
            "rsi_below": f"📊 Buy {symbol} if RSI(14) falls below **?** (e.g. 30)",
        }[ctype]
        await self._edit(query, prompt, parse_mode="Markdown")
        return THRESHOLD

    async def on_threshold(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        val = self._parse_number(update.message.text)
        ctype = context.user_data.get("ctype")
        if val is None or val <= 0 or (ctype == "drop_pct" and val > 100):
            hint = "a percentage between 0 and 100" if ctype == "drop_pct" else "a positive number"
            await self._send(update.effective_chat.id, f"That's not {hint}. Try again or /cancel.")
            return THRESHOLD
        context.user_data["threshold"] = val
        return await self._ask_amount(update, context)

    async def _ask_amount(self, update_or_query, context: ContextTypes.DEFAULT_TYPE) -> int:
        chat_id = (update_or_query.effective_chat.id if hasattr(update_or_query, "effective_chat")
                   else update_or_query.message.chat.id)
        user = self.db.get_user(chat_id)
        budget = user.budget if user else 1000.0
        await self._send(chat_id, f"💰 Maximum amount to invest per trade? (budget: ${budget:,.0f})")
        return AMOUNT

    async def on_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        val = self._parse_number(update.message.text)
        if val is None or val <= 0 or val > MAX_AMOUNT:
            await self._send(update.effective_chat.id, "Enter an amount between $1 and $1,000,000.")
            return AMOUNT
        context.user_data["amount"] = val
        return await self._confirm(update, context)

    async def _confirm(self, update_or_query, context: ContextTypes.DEFAULT_TYPE) -> int:
        draft = RuleDraft(
            symbol=context.user_data["symbol"],
            condition_type=ConditionType(context.user_data["ctype"]),
            threshold=context.user_data.get("threshold"),
            max_amount=context.user_data.get("amount"),
        )
        context.user_data["draft"] = draft
        chat_id = (update_or_query.effective_chat.id if hasattr(update_or_query, "effective_chat")
                   else update_or_query.message.chat.id)
        user = self.db.get_user(chat_id)
        budget = user.budget if user else 1000.0
        text, kb = self.cards.confirm_rule(draft, budget)
        await self._send(chat_id, text, reply_markup=kb, parse_mode="Markdown")
        return CONFIRM

    async def on_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        if query.data == "draft:no":
            await self._edit(query, "Canceled. What next? /search /rules /help")
            return ConversationHandler.END
        draft = context.user_data.get("draft")
        if not draft:
            await self._edit(query, "Session expired. Start again with /search.")
            return ConversationHandler.END
        rule = self.db.create_rule(update.effective_chat.id, draft)
        await self._edit(query, self.cards.rule_saved(rule), parse_mode="Markdown")
        return ConversationHandler.END

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data.clear()
        await self._send(update.effective_chat.id, "Canceled. What next? /search /rules /help")
        return ConversationHandler.END

    async def btn_cancel_wizard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        context.user_data.clear()
        await self._edit(query, "Canceled. What next? /search /rules /help")
        return ConversationHandler.END

    # -- free-text /watch ----------------------------------------------------
    async def cmd_watch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        text = (update.message.text or "").removeprefix("/watch").strip()
        if not text:
            await self._send(update.effective_chat.id,
                             "Usage: `/watch apple under 180 max 250`\n"
                             "I'll parse it with the LLM and confirm before saving.")
            return ConversationHandler.END
        if not self.watch_parser.available:
            await self._send(update.effective_chat.id,
                             "Google ADK not configured (GOOGLE_API_KEY). Use /search to build a rule instead.")
            return ConversationHandler.END
        await self._send(update.effective_chat.id, "🤖 Parsing your rule…")
        draft = await self.watch_parser.parse(text)
        if not draft:
            await self._send(update.effective_chat.id,
                             "Couldn't parse that. Try: `/watch apple under 180 max 250` — or use /search.")
            return ConversationHandler.END
        context.user_data["draft"] = draft
        user = self.db.get_user(update.effective_chat.id)
        budget = user.budget if user else 1000.0
        card, kb = self.cards.confirm_rule(draft, budget)
        await self._send(update.effective_chat.id, card, reply_markup=kb, parse_mode="Markdown")
        return CONFIRM

    # -- proposal actions ----------------------------------------------------
    async def on_proposal_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        _, pid, action = query.data.split(":")
        proposal = self.db.get_proposal(int(pid))
        if not proposal or proposal.status != ProposalStatus.PROPOSED:
            await self._edit(query, "This proposal is no longer active.")
            return
        if action == "pay":
            await self._edit(
                query,
                f"💳 **Pay ${proposal.amount:,.2f} for {proposal.symbol}**\n\n"
                f"Open the PayPal invoice (sandbox) and pay:\n{proposal.pay_link}\n\n"
                "I'll execute the paper trade as soon as payment is confirmed.",
                disable_web_page_preview=True)
        elif action == "reject":
            try:
                await self.paypal.cancel_invoice(proposal.invoice_id)
            except Exception:
                log.exception("cancel invoice %s failed", proposal.invoice_id)
            self.db.update_proposal(proposal.id, status=ProposalStatus.REJECTED)
            rule = self.db.get_rule(proposal.rule_id)
            if rule:
                self.db.update_rule(rule.id, status=RuleStatus.ACTIVE)
            await self._edit(query, "❌ Rejected. The invoice was cancelled; the rule stays active "
                                    "(with a 24h cooldown).")

    async def on_modify_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        _, pid, _ = query.data.split(":")
        proposal = self.db.get_proposal(int(pid))
        if not proposal or proposal.status != ProposalStatus.PROPOSED:
            await self._edit(query, "This proposal is no longer active.")
            return ConversationHandler.END
        context.user_data["modify_proposal_id"] = proposal.id
        await self._edit(query, '✏️ What should change? E.g. "max $200 instead", '
                                '"raise threshold to 190", "lower the amount".')
        return MODIFY

    async def on_modify_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        proposal = self.db.get_proposal(context.user_data.get("modify_proposal_id") or 0)
        if not proposal:
            await self._send(update.effective_chat.id, "Proposal expired.")
            return ConversationHandler.END
        rule = self.db.get_rule(proposal.rule_id)
        if not rule:
            await self._send(update.effective_chat.id, "Rule not found.")
            return ConversationHandler.END
        await self._send(update.effective_chat.id, "🤖 Updating the rule and re-researching…")
        updated = await self.rule_modifier.modify(rule, update.message.text)
        if updated is None:
            await self._send(update.effective_chat.id,
                             'Couldn\'t parse that change. Keep it simple, e.g. "max $200" or '
                             '"raise threshold to 190".')
            return MODIFY
        try:
            await self.paypal.cancel_invoice(proposal.invoice_id)
        except Exception:
            log.exception("cancel invoice %s failed", proposal.invoice_id)
        self.db.update_proposal(proposal.id, status=ProposalStatus.CANCELLED)
        self.db.update_rule(rule.id, symbol=updated.symbol, condition_type=updated.condition_type,
                            threshold=updated.threshold, max_amount=updated.max_amount,
                            status=RuleStatus.ACTIVE, cooldown_until=None)
        context.user_data.clear()
        if self.monitor:
            await self.monitor.trigger(self.db.get_rule(rule.id))
        return ConversationHandler.END

    # -- outbound notifications (used by services) ---------------------------
    async def send_proposal(self, proposal, rule: Rule, brief) -> None:
        text, kb = self.cards.proposal(proposal, rule, brief)
        await self._send(proposal.chat_id, text, reply_markup=kb, parse_mode="Markdown")

    async def send_alert(self, chat_id: int, text: str) -> None:
        await self._send(chat_id, text, parse_mode="Markdown")

    async def notify_fill(self, proposal, execution) -> None:
        await self._send(proposal.chat_id, self.cards.fill_receipt(proposal, execution),
                         parse_mode="Markdown")

    async def notify_error(self, chat_id: int, text: str) -> None:
        await self._send(chat_id, text)