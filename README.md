# StockWatch AI

A constraint-triggered investment agent in Telegram, built for the **PayPal AI
Hackathon** (deadline Nov 12, 2026).

> **No real money, ever.** PayPal **sandbox** invoices are the consent boundary,
> Alpaca **paper trading** executes the trades, and Google **ADK** (Gemini)
> does the research and explanation. The payment *is* the approval — the agent
> never buys on its own.

## How it works

1. You set a rule in Telegram: *"buy AAPL when price < $180, max $300"* — either
   free-text (`/watch apple under 180 max 250`) or by browsing the market
   (`/search` → detail card → condition wizard).
2. The server polls the market every 5 minutes. When your condition fires it
   researches (market facts + news headlines + sentiment from **free keyless
   endpoints** — yfinance + Google News RSS), scores each of your criteria
   PASS/FAIL, and asks the **Google ADK agent** (Gemini 2, no tools) for an
   investment brief written from that data.
3. A proposal card appears with **💳 Pay via PayPal** — a sandbox invoice whose
   line item names the stock and whose description is the full brief.
4. You pay (sandbox login), **✏️ modify** (re-research + new invoice), or
   **❌ reject** (invoice cancelled, rule stays active with a 24h cooldown).
5. The `INVOICING.INVOICE.PAID` webhook is signature-verified and
   amount-checked → Alpaca places a fractional **paper** order → a fill receipt
   lands in the chat.

## Architecture

```
Telegram (UI only) ── python-telegram-bot (long polling + JobQueue)
        │
FastAPI (single asyncio process)
   ├── MonitorService   — polls rules every 5 min
   ├── ResearchService  — market facts + criteria scoring
   │     └── AdkResearchAgent — Google ADK (Gemini 2) writes the brief from free-endpoint data
   ├── WebhookService   — verify → dedupe → amount-check → execute
   ├── ExecutorService  — Alpaca paper order (or simulated fill)
   ├── SQLite           — rules, proposals, executions (per-chat isolated)
        │                          │
   PayPal REST (sandbox)      Alpaca paper API
   invoices + webhooks        fractional market orders
```

Data comes from **free, keyless endpoints** only (yfinance for quotes/history,
Google News RSS for headlines) — no web-search tool is involved, so nothing in
the loop depends on unreliable search availability. **Google ADK is the only
AI layer**: one tool-less Gemini agent per task (research brief, `/watch`
parsing, modify loop).

Every task is a class: `MarketData`, `NewsProvider`, `SentimentAnalyzer`,
`PayPalClient`, `AlpacaClient`, `AdkAgent`, `AdkResearchAgent`,
`WatchCommandParser`, `RuleModifier`, `ResearchService`, `MonitorService`,
`ExecutorService`, `WebhookService`, `CardBuilder`, `TelegramBot`,
`RuleStateMachine`, `ProposalStateMachine`. Standard libraries do the heavy
lifting (yfinance, feedparser, pandas, google-adk, alpaca-py, httpx,
python-telegram-bot, FastAPI) — custom code is orchestration only.

## Quick start (10 minutes)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in the keys below
```

| Key | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | `@BotFather` → `/newbot` |
| `PAYPAL_CLIENT_ID/SECRET` | developer.paypal.com → Apps & Credentials → **Sandbox** app |
| `PAYPAL_WEBHOOK_ID` | the same app → Webhooks → add `https://<your-url>/webhooks/paypal` (event: `INVOICING.INVOICE.PAID`) |
| `PAYPAL_MERCHANT_EMAIL` | your sandbox **business** account email |
| `PAYPAL_RECIPIENT_EMAIL` | your sandbox **personal** account email (the "buyer") |
| `ALPACA_API_KEY/SECRET` | alpaca.markets → paper trading → API keys |
| `GOOGLE_API_KEY` | aistudio.google.com → API key (powers all ADK agents) |

Run it:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- The bot uses **long polling** — no public URL needed for Telegram.
- The PayPal webhook needs a public HTTPS URL:
  `ngrok http 8000` → register `https://<ngrok>.ngrok.app/webhooks/paypal` in
  the PayPal developer dashboard.
- Set `SIMULATE_ORDER=true` to demo **off-market-hours**: fills are computed
  from live quotes instead of placing real paper orders.

### Manual demo script

1. Create sandbox accounts (business + personal) in developer.paypal.com and
   fund the personal one with a test balance.
2. In Telegram: `/start` → `/budget 1000` → `/search` → "apple" → pick AAPL →
   **Buy if price below…** → threshold `999` (fires immediately) → max `300` →
   save.
3. Within a poll cycle the proposal card arrives → **💳 Pay** → open the pay
   link → log in as the sandbox personal account → pay.
4. Watch the webhook fire (check logs) → fill receipt in chat → verify the
   order under Alpaca paper trading → `/history` shows the receipt.

## Verification

```bash
pytest tests/                       # state machine + webhook validation, no network
python scripts/smoke_flow.py        # end-to-end: rule → propose → pay → execute (all fakes)
```

## Configuration

See `.env.example`. Never commit `.env` — credentials come from the environment
only. `ALLOWED_CHAT_IDS` is an allowlist (comma-separated chat ids; empty =
allow all, for local dev).

## Security

- Webhook signatures verified against `PAYPAL_WEBHOOK_ID` via PayPal's
  verify-webhook-signature API; events deduplicated by id.
- Invoice amount must match the proposal amount before any execution.
- Execution happens only after payment confirmation — the payment is the
  consent boundary.
- ADK-parsed rules are always confirmed by the user before saving; numeric
  input is range-checked.

## Deliberate simplifications

- Polling (5 min) instead of push — market data has no webhooks.
- Single process + SQLite — no queues/workers (fine for a demo; upgrade path =
  extract workers + a real DB).
- `yfinance` is unofficial — isolated behind the `MarketData` interface.
- Sentiment is a headline-keyword heuristic — upgrade path: a real model.
- ADK agents need `GOOGLE_API_KEY` and a Gemini model; without one, research
  and parsing fall back to deterministic logic so the money loop still works.

## License

MIT (add a LICENSE file when you publish the repo for submission).