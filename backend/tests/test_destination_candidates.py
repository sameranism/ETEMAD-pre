"""Recovered P23 candidate lifecycle; no database or provider calls."""
import json

import pytest

from backend.modules.publishing.destinations import (
    Destination,
    candidate,
    directory_view,
    disable_candidate,
    update_candidate,
)


def _candidate(**overrides):
    args = {"platform": "telegram", "name": "Fixture", "address": "@fixture_news",
            "credential_ref": "secrets/telegram/fixture", "streams": ["domestic"],
            "desks": ["economy"]}
    return candidate(**{**args, **overrides})


def test_candidate_round_trip_and_redaction():
    record = _candidate()
    restored = json.loads(json.dumps(record))
    assert record == restored
    assert _candidate()["destination_id"] == record["destination_id"]
    assert restored["enabled"] is False
    assert restored["connection_state"] == "unverified"
    assert directory_view(restored)["has_credential_ref"] is True
    assert "credential_ref" not in directory_view(restored)
    assert not Destination(restored, "fixture").eligible(stream="domestic", desk="economy")


def test_update_and_disable_never_activate():
    record = _candidate()
    updated = update_candidate(record, name="New fixture", credential_ref=None,
                               streams=["foreign"], desks=[])
    assert updated["destination_id"] == record["destination_id"]
    assert updated["connection_state"] == "unverified"
    assert updated["enabled"] is False
    disabled = disable_candidate(updated)
    assert disabled["destination_id"] == record["destination_id"]
    assert disabled["connection_state"] == "disabled"
    assert not Destination(disabled, "fixture").eligible(stream="foreign", desk=None)


@pytest.mark.parametrize("override", [
    {"platform": "website"},
    {"platform": "other"},
    {"address": "https://example.invalid/"},
    {"streams": []},
    {"streams": ["domestic", "domestic"]},
    {"desks": ["bad desk"]},
    {"credential_ref": "raw-token-not-a-reference"},
])
def test_invalid_candidate_rejected(override):
    with pytest.raises(ValueError):
        _candidate(**override)
