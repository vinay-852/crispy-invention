import json
import logging
import uuid

import httpx

from ..config import Settings

log = logging.getLogger(__name__)


class PayPalError(RuntimeError):
    pass


class PayPalClient:
    """PayPal REST (sandbox by default): invoices, send/cancel, webhook verification.

    The official Python SDK is outdated, so this speaks the REST API directly
    via httpx (per the project spec).
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._http = httpx.AsyncClient(timeout=30)
        self._token: str | None = None

    async def close(self) -> None:
        await self._http.aclose()

    async def _access_token(self) -> str:
        if self._token:
            return self._token
        resp = await self._http.post(
            f"{self.settings.paypal_base_url}/v1/oauth2/token",
            auth=(self.settings.paypal_client_id, self.settings.paypal_client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json", "Accept-Language": "en_US"},
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    async def create_invoice(self, symbol: str, amount: float, description: str,
                             currency: str = "USD") -> dict:
        """Creates an invoice; the line-item title names the stock, the description is the brief."""
        await self._access_token()
        payload = {
            "detail": {
                "invoice_number": f"SW-{uuid.uuid4().hex[:10].upper()}",
                "currency_code": currency,
                "note": "Paper-trade investment via StockWatch AI (PayPal sandbox — no real money).",
                "payment_term": {"term_type": "DUE_ON_RECEIPT"},
            },
            "invoicer": {
                "email_address": self.settings.paypal_merchant_email,
                "name": {"given_name": "StockWatch", "surname": "AI"},
            },
            "primary_recipients": [
                {"billing_info": {"email_address": self.settings.paypal_recipient}}
            ],
            "items": [
                {
                    "name": f"Invest ${amount:,.2f} in {symbol}",
                    "description": description[:4000],
                    "quantity": "1",
                    "unit_amount": {"currency_code": currency, "value": f"{amount:.2f}"},
                }
            ],
            "configuration": {"partial_payment": {"allow_partial_payment": False}, "allow_tip": False},
        }
        resp = await self._http.post(
            f"{self.settings.paypal_base_url}/v2/invoicing/invoices",
            headers=self._auth_headers(), json=payload,
        )
        if resp.status_code >= 400:
            raise PayPalError(f"create invoice failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    async def send_invoice(self, invoice_id: str) -> None:
        resp = await self._http.post(
            f"{self.settings.paypal_base_url}/v2/invoicing/invoices/{invoice_id}/send",
            headers=self._auth_headers(),
            json={"send_to_invoicer": False, "send_to_recipient": True},
        )
        if resp.status_code >= 400:
            raise PayPalError(f"send invoice failed ({resp.status_code}): {resp.text[:500]}")

    async def cancel_invoice(self, invoice_id: str | None) -> None:
        if not invoice_id:
            return
        resp = await self._http.put(
            f"{self.settings.paypal_base_url}/v2/invoicing/invoices/{invoice_id}/cancel",
            headers=self._auth_headers(),
            json={"subject": "Proposal cancelled",
                  "note": "The proposal was rejected or modified.",
                  "send_to_invoicer": False, "send_to_recipient": True},
        )
        if resp.status_code not in (200, 204):
            log.warning("cancel invoice %s failed: %s %s", invoice_id, resp.status_code, resp.text[:300])

    def pay_link_for(self, invoice_id: str, data: dict) -> str:
        for link in data.get("links", []):
            if link.get("rel") == "pay":
                return link["href"]
        return f"{self.settings.paypal_pay_host}/invoice/p/{invoice_id}"

    async def create_invoice_and_send(self, symbol: str, amount: float, description: str) -> tuple[str, str]:
        data = await self.create_invoice(symbol, amount, description)
        invoice_id = data["id"]
        await self.send_invoice(invoice_id)
        return invoice_id, self.pay_link_for(invoice_id, data)

    async def verify_webhook(self, headers: dict, body: bytes) -> bool:
        """Verify a webhook via PayPal's official verify-webhook-signature API."""
        if not self.settings.paypal_webhook_id:
            log.warning("PAYPAL_WEBHOOK_ID not set — skipping signature verification")
            return True
        try:
            await self._access_token()
            payload = {
                "auth_algo": headers["paypal-auth-algo"],
                "cert_url": headers["paypal-cert-url"],
                "transmission_id": headers["paypal-transmission-id"],
                "transmission_sig": headers["paypal-transmission-sig"],
                "transmission_time": headers["paypal-transmission-time"],
                "webhook_id": self.settings.paypal_webhook_id,
                "webhook_event": json.loads(body),
            }
            resp = await self._http.post(
                f"{self.settings.paypal_base_url}/v1/notifications/verify-webhook-signature",
                headers=self._auth_headers(), json=payload,
            )
            resp.raise_for_status()
            return resp.json().get("verification_status") == "SUCCESS"
        except Exception as e:
            log.warning("webhook verification failed: %s", e)
            return False