"""Verify opt-in runtime composition without any external request."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from backend.app.editorial_runtime import build_editorial_runtime, build_workflow_fixture_runtime

EXAMPLES = Path(__file__).resolve().parents[2] / "source/contracts/examples"


def example(name):
    return json.loads((EXAMPLES / (name + ".example.json")).read_text())


def test_editorial_runtime_connects_routes_and_source_scope():
    engine = create_engine("postgresql+psycopg://fixture_admin@127.0.0.1:1/postgres")
    try:
        assert build_editorial_runtime({}, None, private_identity=False, persistent_sources=False) is None
        tasks = [
            {"task": name, "provider": "avalai", "model": "fixture-" + name,
             "credential_ref": "models/avalai", "max_daily_cost_usd": 1}
            for name in ("translation", "editorial")
        ]
        tariff = {"input_price_per_million": 1, "output_price_per_million": 2,
                  "price_valid_until": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                  "max_input_tokens": 4096, "max_output_tokens": 1024,
                  "output_limit_field": "max_tokens", "run_budget_usd": .01, "timeout_s": 30}
        config = {"models": {**example("ai_models_config"), "tasks": tasks},
                  "routes": {name: tariff for name in ("translation", "editorial")},
                  "monthly_budget_usd": 5,
                  "profiles": [{"ruleset": example("editorial_rules"), "framework": example("news_framework")}],
                  "approved_source_ids": ["SRC-APPROVED"]}
        env = {"ETEMAD_EDITORIAL_AI_MODE": "live", "ETEMAD_EDITORIAL_AI_CONFIG": json.dumps(config)}
        with pytest.raises(ValueError, match="not authorized"):
            build_editorial_runtime(env, engine, private_identity=True, persistent_sources=True)
        env["ETEMAD_EDITORIAL_AI_AUTHORIZED"] = "yes"
        with pytest.raises(ValueError, match="persistent staff"):
            build_editorial_runtime(env, engine, private_identity=False, persistent_sources=True)
        generator = build_editorial_runtime(env, engine, private_identity=True, persistent_sources=True)
        assert generator is not None
        raw = {"stream": "foreign", "language": "en", "reuse_permission": "licensed", "source_id": "SRC-APPROVED"}
        assert generator.available(raw, "international")
        assert not generator.available({**raw, "source_id": "SRC-OTHER"}, "international")
        assert not generator.available({**raw, "reuse_permission": "unverified"}, "international")
        assert not generator.available(raw, "other")
    finally:
        engine.dispose()


def test_workflow_fixture_runtime_is_private_persistent_and_zero_cost():
    config = {"profiles": [{
        "ruleset": example("editorial_rules"),
        "framework": example("news_framework"),
    }]}
    env = {
        "ETEMAD_ENV": "staging",
        "ETEMAD_FEED_MODE": "fixture",
        "ETEMAD_EDITORIAL_AI_MODE": "off",
        "ETEMAD_WORKFLOW_FIXTURE_AUTHORIZED": "yes",
        "ETEMAD_WORKFLOW_FIXTURE_CONFIG": json.dumps(config),
    }
    with pytest.raises(ValueError, match="workflow fixture|invalid authorized"):
        build_workflow_fixture_runtime({}, private_identity=True, persistent_sources=True)
    with pytest.raises(ValueError, match="workflow fixture|invalid authorized"):
        build_workflow_fixture_runtime(env, private_identity=False, persistent_sources=True)
    with pytest.raises(ValueError, match="workflow fixture|invalid authorized"):
        build_workflow_fixture_runtime(
            {**env, "ETEMAD_EDITORIAL_AI_MODE": "live"},
            private_identity=True,
            persistent_sources=True,
        )

    generator = build_workflow_fixture_runtime(
        env, private_identity=True, persistent_sources=True,
    )
    raw = {
        "schema_version": "1.0",
        "news_id": "NEWS-WORKFLOW-FIXTURE",
        "stream": "foreign",
        "source_id": "SRC-WORKFLOW-FIXTURE",
        "source_country": "DE",
        "language": "en",
        "original_url": "https://example.invalid/workflow-fixture",
        "original_title": "Fixture workflow",
        "original_text": 'Alice said “12 units”.',
        "subject_countries": [],
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "content_scope": "summary",
        "reuse_permission": "unverified",
    }
    assert generator.mode == "fixture"
    assert generator.available(raw, "international")
    generated = generator.generate(raw, desk="international")
    assert generated.mode == "fixture"
    assert generated.translation is not None
    assert generated.translation.record["translated_text"] == raw["original_text"]
    assert generated.translation.provenance["provider"] == "fixture"
    assert generated.translation.provenance["cost_usd"] == 0.0
    assert generated.article.record["source_refs"] == [raw["news_id"]]
    assert generated.article.provenance["model"] == "fixture-editorial"
    assert generated.article.provenance["cost_usd"] == 0.0
