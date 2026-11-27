"""P23 scoped, validated destinations; credentials remain references."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from backend.core.contracts import validate_payload


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
