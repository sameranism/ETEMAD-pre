"""Opt-in newsroom composition of the existing translation/editorial services.

The runtime JSON contains only approved routes, tariffs, rules and source IDs.
Credential values are resolved separately, by reference. No default live model
or permissive source policy is supplied. No network request during startup.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Mapping

from sqlalchemy.engine import Engine

from backend.core.ai_cost_ledger import PostgresCostLedger
from backend.core.ai_gateway import Gateway, ModelBinding, Provider
from backend.core.chat_provider import ChatProvider, ChatRoute
from backend.core.contracts import validate_payload
from backend.core.secrets import EnvSecretStore
from backend.modules.editorial.generation import EditorialGenerator, EditorialProfile


def build_editorial_runtime(
    environ: Mapping[str, str], engine: Engine | None, *, private_identity: bool,
    persistent_sources: bool,
) -> EditorialGenerator | None:
    mode = environ.get("ETEMAD_EDITORIAL_AI_MODE", "off")
    if mode == "off":
        return None
    if mode != "live" or engine is None or not private_identity or not persistent_sources:
        raise ValueError("live editorial AI requires persistent staff identity and source storage")
    if environ.get("ETEMAD_EDITORIAL_AI_AUTHORIZED") != "yes":
        raise ValueError("live editorial AI is not authorized")
    try:
        raw = environ.get("ETEMAD_EDITORIAL_AI_CONFIG", "")
        if not raw or len(raw) > 500_000:
            raise ValueError("invalid configuration size")
        config = json.loads(raw)
        if set(config) != {"models", "routes", "monthly_budget_usd", "profiles", "approved_source_ids"}:
            raise ValueError("invalid runtime configuration")
        validate_payload("ai_models_config", config["models"])
        tasks = config["models"]["tasks"]
        if len(tasks) != 2 or {item["task"] for item in tasks} != {"translation", "editorial"}:
            raise ValueError("translation and editorial bindings required")
        budget = config["monthly_budget_usd"]
        if type(budget) not in {float, int} or not 0 < budget <= 1_000_000:
            raise ValueError("invalid monthly budget")
        secrets = EnvSecretStore(environ)
        bindings: dict[str, ModelBinding] = {}
        routes: dict[str, dict[str, ChatRoute]] = {}
        tariffs = config["routes"]
        if set(tariffs) != {"translation", "editorial"}:
            raise ValueError("missing task tariff")
        for item in tasks:
            task, provider, model = item["task"], item["provider"], item["model"]
            values = dict(tariffs[task])
            if set(values) != {"input_price_per_million", "output_price_per_million", "price_valid_until",
                               "max_input_tokens", "max_output_tokens", "output_limit_field",
                               "run_budget_usd", "timeout_s"}:
                raise ValueError("invalid tariff")
            run_budget, timeout = values.pop("run_budget_usd"), values.pop("timeout_s")
            if (type(run_budget) not in {float, int} or type(timeout) not in {float, int}
                    or not 0 < run_budget <= item["max_daily_cost_usd"] <= budget or not 0 < timeout <= 120):
                raise ValueError("invalid task budget")
            values["price_valid_until"] = datetime.fromisoformat(values["price_valid_until"])
            route = ChatRoute(model=model, credential_ref=item["credential_ref"], **values)
            if route.cost(route.max_input_tokens, route.max_output_tokens) > run_budget:
                raise ValueError("tariff exceeds task budget")
            provider_routes = routes.setdefault(provider, {})
            if model in provider_routes and route != provider_routes[model]:
                raise ValueError("conflicting model route")
            provider_routes[model] = route
            bindings[task] = ModelBinding(task, provider, model, timeout, run_budget, item["max_daily_cost_usd"])
        providers: dict[str, Provider] = {
            name: ChatProvider(name, route_map, secrets,
                               authorized=lambda: environ.get("ETEMAD_EDITORIAL_AI_AUTHORIZED") == "yes")
            for name, route_map in routes.items()
        }
        profiles: dict[tuple[str, str], EditorialProfile] = {}
        for profile in config["profiles"]:
            if set(profile) != {"ruleset", "framework"}:
                raise ValueError("invalid editorial profile")
            frame = profile["framework"]
            scope = (frame["stream"], frame["desk"])
            if scope in profiles or scope[0] not in {"domestic", "foreign", "manual"}:
                raise ValueError("duplicate or invalid profile scope")
            profiles[scope] = EditorialProfile(profile["ruleset"], frame)
        approved = config["approved_source_ids"]
        if (not profiles or not isinstance(approved, list) or not approved
                or len(approved) > 1000 or any(not isinstance(x, str) or not x.strip() or len(x) > 128 for x in approved)):
            raise ValueError("approved source scope is required")
        gateway = Gateway(bindings, providers, enabled=True, monthly_budget_usd=budget,
                          cost_ledger=PostgresCostLedger(engine))
        return EditorialGenerator(gateway, profiles, mode="live", approved_source_ids=frozenset(approved))
    except (ValueError, KeyError, TypeError, AttributeError):
        # Never echo runtime JSON, references, or underlying provider errors.
        raise ValueError("invalid authorized editorial AI configuration") from None
