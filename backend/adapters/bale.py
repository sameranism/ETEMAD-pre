"""P25/S06-007 Bale Bot API text transport, not registered in fixture dispatch.

Official API: https://docs.bale.ai/ (ordinary bot API, sendMessage).
No business/paid bulk API, redirects, automatic retry or token logging.
Live authorization and verified destination must be supplied by a trusted
composition; the caller must reserve the durable publication attempt first.
"""
from __future__ import annotations

import http.client
import json
import re
from typing import Any, Callable, Mapping

from backend.adapters.base import Delivery
from backend.core.contracts import validate_payload
from backend.core.secrets import SecretStore, validate_ref

Transport = Callable[[str, Mapping[str, Any]], tuple[int, bytes]]
_TOKEN = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,100}")
_CHAT = re.compile(r"(?:-?[0-9]{1,20}|@[A-Za-z0-9_]{5,32})")
_MAX_RESPONSE = 1_048_576


def _send(token: str, data: Mapping[str, Any]) -> tuple[int, bytes]:
    conn = http.client.HTTPSConnection("tapi.bale.ai", timeout=15)
    try:
        conn.request("POST", f"/bot{token}/sendMessage",
                     body=json.dumps(data, ensure_ascii=False).encode("utf-8"),
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        body = response.read(_MAX_RESPONSE + 1)
        if len(body) > _MAX_RESPONSE:
            raise ValueError("provider response exceeds bound")
        return response.status, body
    finally:
        conn.close()


class BaleAdapter:
    platform = "bale"
    mode = "live"
    max_text_chars = 4096

    def __init__(self, adapter_id: str, config: Mapping[str, Any], secrets: SecretStore,
                 *, authorized: Callable[[], bool] | None = None,
                 transport: Transport = _send) -> None:
        if not adapter_id.strip():
            raise ValueError("explicit adapter identity required")
        self.adapter_id = adapter_id
        self._secrets = secrets
        self._authorized = authorized or (lambda: False)
        self._transport = transport
        self.validate_config(config)
        self._destination_id = str(config["destination_id"])
        self._chat_id = str(config["address"])
        self._credential_ref = str(config["credential_ref"])

    def validate_config(self, config: Mapping[str, Any]) -> None:
        validate_payload("destination_config", config)
        if (config["platform"] != self.platform or not config["enabled"]
                or config["connection_state"] != "verified"
                or not _CHAT.fullmatch(str(config["address"]))
                or not config["credential_ref"]):
            raise ValueError("Bale destination is not verified")
        validate_ref(str(config["credential_ref"]))
        if hasattr(self, "_destination_id") and (
                config["destination_id"], config["address"], config["credential_ref"]
        ) != (self._destination_id, self._chat_id, self._credential_ref):
            raise ValueError("destination binding changed")

    def dry_run(self, payload: Mapping[str, Any]) -> None:
        validate_payload("destination_payload", payload)
        material = payload["text"]
        if (payload["destination_id"] != self._destination_id or payload["content_type"] != "text"
                or payload["asset_uris"] or not isinstance(material, str) or not material.strip()
                or len(material.encode("utf-16-le")) // 2 > self.max_text_chars):
            raise ValueError("invalid Bale text payload")
        # Bale always interprets its Markdown syntax. Hold possible formatting
        # rather than silently changing approved newsroom text or links.
        if any(marker in material for marker in ("**", "__", chr(96), "\\")) or re.search(
            r"\[[^\]]*\]\s*\(", material
        ):
            raise ValueError("Bale text requires an explicit formatting policy")

    def publish(self, payload: Mapping[str, Any], *, idempotency_key: str) -> Delivery:
        self.dry_run(payload)
        if not idempotency_key.strip():
            raise ValueError("publication key is required")
        if self._authorized() is not True:
            return Delivery("skipped", error_code="LIVE_NOT_AUTHORIZED")
        token = self._secrets.get(self._credential_ref)
        if not token or not _TOKEN.fullmatch(token):
            return Delivery("failed", error_code="CREDENTIAL_UNAVAILABLE")
        if self._authorized() is not True:
            return Delivery("skipped", error_code="LIVE_NOT_AUTHORIZED")
        try:
            status, body = self._transport(token, {"chat_id": self._chat_id, "text": payload["text"]})
            if len(body) > _MAX_RESPONSE:
                raise ValueError("provider response exceeds bound")
            parsed = json.loads(body)
            if not isinstance(parsed, dict):
                raise ValueError("invalid provider response")
            result = parsed.get("result")
            if status == 200 and parsed.get("ok") is True and isinstance(result, dict):
                message_id = result.get("message_id")
                if type(message_id) is int and message_id > 0:
                    return Delivery("success", str(message_id))
            if status in {400, 401, 403, 404, 429} and parsed.get("ok") is False:
                return Delivery("failed", error_code=f"BALE_{status}")
        except Exception:
            pass  # The provider may have accepted the send; do not retry.
        return Delivery("unknown", error_code="ATTEMPT_UNCONFIRMED")

    def status(self, idempotency_key: str) -> Delivery | None:
        # No documented caller-key lookup in the ordinary bot API.
        return None
