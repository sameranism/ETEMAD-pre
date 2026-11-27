"""S06-007 adapter contract with synthetic responses; never a live request."""
import json

import pytest

from backend.adapters.bale import BaleAdapter
from backend.adapters.registry import AdapterRegistry
from backend.core.secrets import InMemorySecretStore

CONFIG = {
    "schema_version": "1.0", "destination_id": "DST-BALE-FIXTURE",
    "platform": "bale", "name": "Fixture", "address": "@test_channel",
    "credential_ref": "bale/test", "desks": [], "allowed_streams": ["domestic"],
    "enabled": True, "connection_state": "verified", "updated_at": "2026-11-27T00:00:00Z",
}
PAYLOAD = {
    "schema_version": "1.0", "payload_id": "PL-FIXTURE", "job_id": "JOB-FIXTURE",
    "destination_id": CONFIG["destination_id"], "content_type": "text",
    "text": "خبر آزمایشی", "asset_uris": [], "source_refs": ["NEWS-FIXTURE"],
    "prepared_at": "2026-11-27T00:00:00Z",
}


def test_default_off_and_binding():
    calls = []

    def transport(token, body):
        calls.append(body)
        return 200, json.dumps({"ok": True, "result": {"message_id": 42}}).encode()

    secrets = InMemorySecretStore({"bale/test": "123456:" + "a" * 30})
    disabled = BaleAdapter("bale", CONFIG, secrets, transport=transport)
    assert disabled.publish(PAYLOAD, idempotency_key="key").status == "skipped"
    assert not calls
    adapter = BaleAdapter("bale", CONFIG, secrets, authorized=lambda: True, transport=transport)
    with pytest.raises(ValueError):
        adapter.validate_config({**CONFIG, "address": "@other_channel"})
    with pytest.raises(ValueError):
        adapter.publish({**PAYLOAD, "destination_id": "other"}, idempotency_key="key")
    with pytest.raises(ValueError):
        adapter.dry_run({**PAYLOAD, "text": "**formatted**"})
    with pytest.raises(ValueError):
        adapter.dry_run({**PAYLOAD, "text": "a" * 4097})
    with pytest.raises(ValueError):
        BaleAdapter("bale", {**CONFIG, "connection_state": "unverified"}, secrets)
    assert not calls
    assert adapter.publish(PAYLOAD, idempotency_key="key").message_id == "42"
    assert calls == [{"chat_id": "@test_channel", "text": PAYLOAD["text"]}]
    assert adapter.status("key") is None
    with pytest.raises(ValueError):
        AdapterRegistry({"bale": adapter})


@pytest.mark.parametrize("status,body,expected", [
    (400, {"ok": False}, "failed"),
    (429, {"ok": False}, "failed"),
    (500, {"ok": False}, "unknown"),
    (200, {"ok": True, "result": {}}, "unknown"),
    (200, {"ok": True, "result": {"message_id": True}}, "unknown"),
])
def test_response_classification(status, body, expected):
    calls = []

    def transport(token, data):
        calls.append(data)
        return status, json.dumps(body).encode()

    adapter = BaleAdapter("bale", CONFIG, InMemorySecretStore({"bale/test": "123456:" + "a" * 30}),
                          authorized=lambda: True, transport=transport)
    assert adapter.publish(PAYLOAD, idempotency_key="key").status == expected
    assert len(calls) == 1


def test_timeout_and_revoked_permission():
    calls = []

    def transport(token, data):
        calls.append(data)
        raise TimeoutError("synthetic")

    secrets = InMemorySecretStore({"bale/test": "123456:" + "a" * 30})
    authorization = iter((True, False))
    revoked = BaleAdapter("bale", CONFIG, secrets, authorized=lambda: next(authorization),
                          transport=transport)
    assert revoked.publish(PAYLOAD, idempotency_key="key").status == "skipped"
    assert not calls
    live = BaleAdapter("bale", CONFIG, secrets, authorized=lambda: True, transport=transport)
    assert live.publish(PAYLOAD, idempotency_key="key").status == "unknown"
    assert len(calls) == 1
