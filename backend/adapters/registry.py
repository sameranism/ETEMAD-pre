"""P25 explicit registry; never guesses an adapter for an unsupported platform."""
from __future__ import annotations

from typing import Mapping

from backend.adapters.base import PublicationAdapter


class AdapterRegistry:
    def __init__(self, adapters: Mapping[str, PublicationAdapter]) -> None:
        self._adapters = dict(adapters)
        if any(key != adapter.adapter_id or adapter.mode != "fixture"
               or not 1 <= adapter.max_text_chars <= 100_000
               for key, adapter in self._adapters.items()):
            raise ValueError("only explicit bounded fixture adapters are supported")

    def get(self, adapter_id: str, platform: str) -> PublicationAdapter:
        adapter = self._adapters.get(adapter_id)
        if adapter is None or adapter.platform != platform:
            raise ValueError("unsupported destination adapter")
        return adapter
