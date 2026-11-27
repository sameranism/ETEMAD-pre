"""No network; validate official request shapes and fail-closed budgets."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.core.ai_gateway import BeforeProviderCall, BudgetExceeded, Gateway, ModelBinding
from backend.core.chat_provider import ChatProvider, ChatRoute, ProviderResponseError
from backend.core.secrets import InMemorySecretStore


def route(model="approved-model", *, expires=None, limit="max_completion_tokens"):
    return ChatRoute(model, "models/fixture", 1.0, 2.0,
                     expires or datetime.now(timezone.utc) + timedelta(days=1),
                     1024, 256, limit)


@pytest.mark.parametrize("name,url", [
    ("avalai", "https://api.avalai.ir/v1/chat/completions"),
    ("deepseek", "https://api.deepseek.com/chat/completions"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
    ("openai", "https://api.openai.com/v1/chat/completions"),
])
def test_provider_request_and_provenance(name, url):
    calls = []
    field = "max_tokens" if name in {"deepseek", "gemini"} else "max_completion_tokens"

    def transport(actual_url, secret, payload, timeout):
        calls.append(actual_url)
        assert actual_url == url and secret == "fixture-only"
        assert payload["model"] == "approved-model"
        assert payload[field] == 256 and payload["stream"] is False
        assert "tools" not in payload
        return {"usage": {"prompt_tokens": 40, "completion_tokens": 20},
                "choices": [{"finish_reason": "stop", "message": {"content": "پاسخ"}}]}

    p = ChatProvider(name, {"approved-model": route(limit=field)}, InMemorySecretStore({"models/fixture": "fixture-only"}),
                     authorized=lambda: True, transport=transport)
    g = Gateway({"translation": ModelBinding("translation", name, "approved-model", run_budget_usd=.01)},
                {name: p}, enabled=True, monthly_budget_usd=.01)
    result = g.call("translation", "ترجمه")
    assert result.provider == name and result.model == "approved-model"
    assert result.cost_usd == pytest.approx(.00008)
    assert calls == [url]
    assert "fixture-only" not in str(result.provenance())


def test_no_call_without_authorization_or_budget_or_fresh_price():
    calls = []
    for authorized, expires, budget, expected in [
        (False, None, .01, BeforeProviderCall),
        (True, datetime.now(timezone.utc) - timedelta(days=1), .01, BeforeProviderCall),
        (True, None, .0001, BudgetExceeded),
    ]:
        p = ChatProvider("avalai", {"approved-model": route(expires=expires)}, InMemorySecretStore(),
                         authorized=lambda: authorized, transport=lambda *args: calls.append(args))
        g = Gateway({"translation": ModelBinding("translation", "avalai", "approved-model", run_budget_usd=budget)},
                    {"avalai": p}, enabled=True)
        with pytest.raises(expected):
            g.call("translation", "ترجمه")
    assert not calls


def test_unknown_delivery_is_reserved_without_automatic_retry():
    calls = []
    r = route(limit="max_tokens")

    def fail(*args):
        calls.append(1)
        raise TimeoutError("do not echo private details")

    p = ChatProvider("deepseek", {r.model: r}, InMemorySecretStore({"models/fixture": "fixture-only"}),
                     authorized=lambda: True, transport=fail)
    g = Gateway({"chat": ModelBinding("chat", "deepseek", r.model, run_budget_usd=.01)},
                {"deepseek": p}, enabled=True, monthly_budget_usd=.002)
    with pytest.raises(ProviderResponseError, match="unconfirmed"):
        g.call("chat", "خبر")
    assert g.spent_usd == pytest.approx(r.cost(r.max_input_tokens, r.max_output_tokens), abs=1e-6)
    with pytest.raises(BudgetExceeded):
        g.call("chat", "خبر")
    assert calls == [1]


def test_selection_uses_measured_cost_per_accepted_output_and_quality_floor():
    from backend.core.chat_provider import ModelEvaluation, choose_evaluated_model

    cheap_bad = ModelEvaluation("avalai", "cheap", "translation", "fa-v1", 100, 79, 0, .1, 1)
    efficient = ModelEvaluation("avalai", "efficient", "translation", "fa-v1", 100, 97, 0, .4, 2)
    expensive = ModelEvaluation("deepseek", "accurate", "translation", "fa-v1", 100, 99, 0, 1.0, 2)
    unsafe = ModelEvaluation("gemini", "unsafe", "translation", "fa-v1", 100, 99, 1, .01, 1)
    incomparable = ModelEvaluation("openai", "unmatched", "translation", "fa-v2", 100, 100, 0, .01, 1)
    results = [cheap_bad, efficient, expensive, unsafe, incomparable]
    assert choose_evaluated_model(results, task="translation", corpus_id="fa-v1",
        approved_routes={(x.provider, x.model) for x in results}, min_samples=100,
        min_acceptance=.95, max_p95_s=5) == efficient
    assert choose_evaluated_model(results, task="translation", corpus_id="fa-v1",
        approved_routes=set(), min_samples=100, min_acceptance=.95, max_p95_s=5) is None


def test_concurrent_requests_cannot_oversubscribe_process_budget():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    started, release = Event(), Event()
    r = route()

    def transport(*args):
        started.set()
        assert release.wait(5)
        return {"usage": {"prompt_tokens": 40, "completion_tokens": 20},
                "choices": [{"finish_reason": "stop", "message": {"content": "پاسخ"}}]}

    p = ChatProvider("avalai", {r.model: r}, InMemorySecretStore({"models/fixture": "fixture-only"}),
                     authorized=lambda: True, transport=transport)
    g = Gateway({"chat": ModelBinding("chat", "avalai", r.model, run_budget_usd=.01)},
                {"avalai": p}, enabled=True, monthly_budget_usd=.002)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(g.call, "chat", "خبر")
        try:
            assert started.wait(5)
            with pytest.raises(BudgetExceeded):
                g.call("chat", "خبر")
        finally:
            release.set()
        assert future.result(timeout=5).text == "پاسخ"
