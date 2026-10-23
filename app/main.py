import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from .config import Settings
from .db import Database
from .integrations.alpaca import AlpacaClient
from .integrations.paypal import PayPalClient
from .notifiers.cards import CardBuilder
from .notifiers.telegram_bot import TelegramBot
from .providers.market import MarketData
from .providers.news import NewsProvider
from .services.adk_research import AdkResearchAgent
from .services.executor import ExecutorService
from .services.monitor import MonitorService
from .services.research import ResearchService
from .services.webhook import WebhookService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, db=None, paypal=None, executor=None, bot=None) -> FastAPI:
    """Build the app. Dependencies can be overridden for tests (see tests/test_webhook.py)."""
    settings = settings or Settings()
    db = db or Database(settings.db_path)

    market = MarketData()
    news = NewsProvider()
    adk = AdkResearchAgent(model=settings.adk_model)
    research = ResearchService(settings, market, news, adk=adk)
    paypal = paypal or PayPalClient(settings)
    alpaca = AlpacaClient(settings)
    executor = executor or ExecutorService(settings, db, alpaca, market)
    cards = CardBuilder()
    bot = bot or TelegramBot(settings, db, market, news, research, paypal, cards)
    monitor = MonitorService(settings, db, market, news, research, paypal, bot)
    bot.monitor = monitor
    webhook = WebhookService(settings, db, paypal, executor, bot)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.telegram_bot_token:
            await bot.start()
            log.info("bot started (long polling)")
        yield
        if settings.telegram_bot_token:
            await bot.stop()
        if isinstance(paypal, PayPalClient):
            await paypal.close()

    app = FastAPI(title="StockWatch AI", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok", "simulate_order": settings.simulate_order}

    @app.post("/webhooks/paypal")
    async def paypal_webhook(request: Request):
        body = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()
                   if k.lower().startswith("paypal-")}
        # Return 200 immediately; verify/execute in a background task (never block on LLM/orders).
        asyncio.create_task(webhook.process(body, headers))
        return {"status": "received"}

    return app


app = create_app()