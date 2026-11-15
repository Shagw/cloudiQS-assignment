"""Signed, retrying webhook client for notifying the risk team."""

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


@dataclass
class DeliveryResult:
    delivered: bool
    status: int | None = None
    error: str | None = None


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 over "<timestamp>.<body>" so receivers can verify origin and reject replays."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


class WebhookClient:
    def __init__(
        self,
        url: str | None = None,
        secret: str | None = None,
        timeout: float = 5.0,
        max_attempts: int = 3,
        transport: httpx.BaseTransport | None = None,
    ):
        self.url = url or os.environ.get("RISK_WEBHOOK_URL")
        self.secret = secret or os.environ.get("RISK_WEBHOOK_SECRET")
        self.max_attempts = max_attempts
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def send(self, payload: dict) -> DeliveryResult:
        if not self.url:
            return DeliveryResult(False, error="RISK_WEBHOOK_URL is not configured")
        if not self.url.startswith("https://") and not (_is_local(self.url) or _insecure_allowed()):
            return DeliveryResult(False, error="webhook URL must use https")

        body = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode()
        # Same key across retries so the receiver can de-duplicate.
        headers = {"Content-Type": "application/json", "Idempotency-Key": str(uuid.uuid4())}

        last = DeliveryResult(False, error="no attempts made")
        for attempt in range(1, self.max_attempts + 1):
            ts = str(int(time.time()))
            if self.secret:
                headers["X-Risk-Timestamp"] = ts
                headers["X-Risk-Signature"] = sign(self.secret, ts, body)
            try:
                resp = self._client.post(self.url, content=body, headers=headers)
                if resp.is_success:
                    return DeliveryResult(True, status=resp.status_code)
                last = DeliveryResult(False, status=resp.status_code, error=f"HTTP {resp.status_code}")
                if resp.status_code not in RETRYABLE_STATUS:
                    break
            except httpx.HTTPError as e:
                last = DeliveryResult(False, error=type(e).__name__)
            if attempt < self.max_attempts:
                time.sleep(0.5 * 2 ** (attempt - 1))
        log.warning("webhook delivery failed: %s", last.error)
        return last


def _is_local(url: str) -> bool:
    host = httpx.URL(url).host
    return host in {"localhost", "127.0.0.1"}


def _insecure_allowed() -> bool:
    # For a receiver on a private Docker network; never set this in production.
    return os.environ.get("RISK_WEBHOOK_ALLOW_INSECURE") == "true"
