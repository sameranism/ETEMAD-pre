"""P17 measured metrics from complete, authorized producer snapshots."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from backend.core.contracts import validate_payload
from backend.modules.intelligence.evidence import Period


@dataclass(frozen=True)
class MetricResult:
    snapshot: dict[str, Any] | None
    evidence: dict[str, Any]


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def compute_metrics(
    period: Period, *, stream: str, raw: Sequence[Mapping[str, Any]] | None,
    classifications: Sequence[Mapping[str, Any]] | None,
    clusters: Sequence[Mapping[str, Any]] | None,
    publication_jobs: Sequence[Mapping[str, Any]] | None,
    publication_results: Sequence[Mapping[str, Any]] | None,
    source_revisions: Mapping[str, str], complete: bool,
) -> MetricResult:
    if stream not in {"mixed", "domestic", "foreign", "manual"}:
        raise ValueError("invalid metric stream")
    evidence: dict[str, Any] = {"revision": "metrics-v1", "data_sources": dict(source_revisions),
                                "time_range": period.as_record(), "delivery_success_rate": None,
                                "delivery_denominator": None}
    if (not complete or raw is None or classifications is None or clusters is None
            or publication_jobs is None or publication_results is None or
            not all(source_revisions.get(k) for k in ("raw", "processing", "publication"))):
        return MetricResult(None, {**evidence, "reason": "INCOMPLETE_PRODUCER_SNAPSHOT"})

    def indexed(contract: str, key: str, records: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
        output: dict[str, Mapping[str, Any]] = {}
        for record in records:
            validate_payload(contract, record)
            identity = str(record[key])
            if identity in output and output[identity] != record:
                raise ValueError("conflicting metric record")
            output[identity] = record
        return output

    raw_index = indexed("raw_news", "news_id", raw)
    classified = indexed("classified_news", "news_id", classifications)
    cluster_index = indexed("cluster_result", "news_id", clusters)
    jobs = indexed("publication_job", "job_id", publication_jobs)
    results = indexed("publication_result", "job_id", publication_results)
    if any(job["state"] == "completed" and job_id not in results
           and (stream == "mixed" or job["stream"] == stream)
           and _time(job["scheduled_at"]) < period.end for job_id, job in jobs.items()):
        return MetricResult(None, {**evidence, "reason": "PUBLICATION_RESULT_MISSING"})
    selected = {i: record for i, record in raw_index.items()
                if (stream == "mixed" or record["stream"] == stream)
                and period.start <= _time(record["collected_at"]) < period.end}
    if any(i not in classified or i not in cluster_index for i in selected):
        return MetricResult(None, {**evidence, "reason": "PROCESSING_SNAPSHOT_INCOMPLETE"})
    unique = []
    topics: Counter[str] = Counter()
    for i, item in selected.items():
        c, cl = classified[i], cluster_index[i]
        if c["stream"] != item["stream"] or c["event_id"] != cl["event_id"] or cl["duplicate_of"] == i:
            raise ValueError("processing identity mismatch")
        if cl["duplicate_of"] is None:
            unique.append(i)
            topics.update(set(c["topics"]))
    delivery_counts: Counter[str] = Counter()
    published: set[tuple[str, str]] = set()
    for job_id, result in results.items():
        job = jobs.get(job_id)
        if job is None or result["object_id"] != job["object_id"]:
            raise ValueError("publication metric identity mismatch")
        if stream != "mixed" and job["stream"] != stream:
            continue
        seen_dest: set[str] = set()
        for delivery in result["deliveries"]:
            dest = delivery["destination_id"]
            if dest not in job["destination_ids"] or dest in seen_dest:
                raise ValueError("duplicate or unrelated delivery")
            seen_dest.add(dest)
            if period.start <= _time(delivery["attempted_at"]) < period.end:
                status = delivery["status"]
                delivery_counts[status] += 1
                if status == "success":
                    published.add((job["object_type"], job["object_id"]))
    success, failed, unknown = (delivery_counts[k] for k in ("success", "failed", "unknown"))
    denominator = success + failed + unknown
    payload = {"stream": stream, "time_range": period.as_record(),
               "collected_count": len(selected), "unique_count": len(unique),
               "published_count": len(published), "successful_deliveries": success,
               "failed_deliveries": failed, "unknown_deliveries": unknown,
               "topic_coverage": [{"topic": key, "count": val} for key, val in sorted(topics.items())]}
    digest = hashlib.sha256(json.dumps([payload, dict(source_revisions)], sort_keys=True,
                                       ensure_ascii=False).encode("utf-8")).hexdigest()
    snapshot = {"schema_version": "1.0", "snapshot_id": "MET-" + digest[:32], **payload,
                "generated_at": datetime.now(timezone.utc).isoformat()}
    validate_payload("metrics_snapshot", snapshot)
    return MetricResult(snapshot, {**evidence, "delivery_denominator": denominator,
                                   "delivery_success_rate": success / denominator if denominator else None,
                                   "unique_basis": "non_duplicate_news_records", "topic_denominator": len(unique),
                                   "skipped_deliveries": delivery_counts["skipped"]})
