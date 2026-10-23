from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment configuration — validated on load (see .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    allowed_chat_ids: list[int] = []

    paypal_mode: str = "sandbox"
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_webhook_id: str = ""
    paypal_merchant_email: str = ""
    paypal_recipient_email: str = ""

    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""

    # Google ADK agents (Gemini) — the only AI layer: research briefs,
    # /watch parsing and the modify loop. Auth comes from GOOGLE_API_KEY /
    # GEMINI_API_KEY in the environment.
    adk_model: str = "gemini-2.0-flash"

    simulate_order: bool = False
    db_path: str = "storage.sqlite"
    poll_interval_minutes: int = 5
    invoice_expiry_hours: int = 2
    cooldown_hours: int = 24

    @field_validator("allowed_chat_ids", mode="before")
    @classmethod
    def _parse_chat_ids(cls, v):
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v

    @property
    def paypal_base_url(self) -> str:
        return "https://api-m.sandbox.paypal.com" if self.paypal_mode == "sandbox" else "https://api-m.paypal.com"

    @property
    def paypal_pay_host(self) -> str:
        return "https://www.sandbox.paypal.com" if self.paypal_mode == "sandbox" else "https://www.paypal.com"

    @property
    def paypal_recipient(self) -> str:
        return self.paypal_recipient_email or self.paypal_merchant_email