"""P23 scoped, validated destinations; credentials remain references."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from backend.core.contracts import validate_payload
from backend.core.secrets import validate_ref


@dataclass(frozen=True)
class Destination:
    config: Mapping[str, Any]
    adapter_id: str

    def __post_init__(self) -> None:
        record = json.loads(json.dumps(dict(self.config), ensure_ascii=False, allow_nan=False))
        validate_payload("destination_config", record)
        if not self.adapter_id.strip():
            raise ValueError("explicit adapter binding required")
        if record["enabled"] and (record["connection_state"] != "verified" or not record["credential_ref"]):
            raise ValueError("active destination requires verified connection and credential reference")
        object.__setattr__(self, "config", record)

    @property
    def identity(self) -> str:
        return str(self.config["destination_id"])

    def eligible(self, *, stream: str, desk: str | None) -> bool:
        return bool(self.config["enabled"] and self.config["connection_state"] == "verified"
                    and self.config["credential_ref"] and stream in self.config["allowed_streams"]
                    and (not self.config["desks"] or (desk is not None and desk in self.config["desks"])))


_SUPPORTED_CANDIDATES = frozenset({"telegram", "bale", "eitaa", "rubika"})
_ADDRESS = re.compile(r"(?:-?[1-9][0-9]{0,19}|@[A-Za-z0-9_]{5,32})")
_DESK = re.compile(r"[a-z][a-z0-9_-]{0,63}")


def candidate(*, platform: str, name: str, address: str, credential_ref: str | None,
              streams: list[str], desks: list[str]) -> dict[str, Any]:
    """Build only an inactive candidate; a user cannot assert API verification."""
    address = address.strip()
    name = name.strip()
    if (platform not in _SUPPORTED_CANDIDATES or not 1 <= len(name) <= 120
            or not _ADDRESS.fullmatch(address) or not streams
            or len(streams) != len(set(streams)) or not set(streams) <= {"domestic", "foreign"}
            or len(desks) > 20 or len(desks) != len(set(desks))
            or any(not _DESK.fullmatch(desk) for desk in desks)):
        raise ValueError("invalid destination candidate")
    if credential_ref is not None:
        validate_ref(credential_ref)
    identity = hashlib.sha256(json.dumps([platform, address], separators=(",", ":")).encode()).hexdigest()
    record = {
        "schema_version": "1.0", "destination_id": "DST-" + identity[:32],
        "platform": platform, "name": name, "address": address, "credential_ref": credential_ref,
        "desks": sorted(desks), "allowed_streams": sorted(streams),
        "enabled": False, "connection_state": "unverified",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    validate_payload("destination_config", record)
    return record


def directory_view(record: Mapping[str, Any]) -> dict[str, Any]:
    validate_payload("destination_config", record)
    # Never return the secret reference or a credential value in the directory.
    return {**{k: v for k, v in record.items() if k != "credential_ref"},
            "has_credential_ref": record["credential_ref"] is not None}



def update_candidate(record: Mapping[str, Any], *, name: str, credential_ref: str | None,
                     streams: list[str], desks: list[str]) -> dict[str, Any]:
    """A configuration change invalidates verification and never activates delivery."""
    validate_payload("destination_config", record)
    updated = candidate(platform=record["platform"], name=name, address=record["address"],
                        credential_ref=credential_ref, streams=streams, desks=desks)
    if updated["destination_id"] != record["destination_id"]:
        raise ValueError("destination identity mismatch")
    return updated


def disable_candidate(record: Mapping[str, Any]) -> dict[str, Any]:
    """Retain the destination and its identity; never delete or send."""
    validate_payload("destination_config", record)
    result = {**record, "enabled": False, "connection_state": "disabled",
              "updated_at": datetime.now(timezone.utc).isoformat()}
    validate_payload("destination_config", result)
    return result
