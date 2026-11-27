"""P19 encrypted, separately scoped executive report repository.

The normal application connection cannot read the executive index. Only the
dedicated DB role may read and append its metadata; report bodies and evidence
are authenticated ciphertext in a disjoint object store. The key stays outside
both databases and object storage. The existing report API owns user ACL.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, make_url

from backend.core.contracts import validate_payload
from backend.core.object_store import LocalObjectStore

_MAGIC = b"ETEMAD-P19-v1\x00"
_MAX_CONTENT = 2 * 1024 * 1024


class ExecutiveStorageUnavailable(RuntimeError):
    """Private storage is not configured or its isolation cannot be verified."""


def _identity(conn: Connection) -> dict[str, Any]:
    return dict(conn.execute(text("""
        SELECT current_user AS role,current_database() AS database,
               r.rolsuper,r.rolbypassrls,r.rolcreaterole,r.rolcreatedb,
               has_schema_privilege(current_user,n.oid,'USAGE') AS private_usage,
               has_schema_privilege(current_user,n.oid,'CREATE') AS private_create,
               has_table_privilege(current_user,p.oid,'SELECT') AS private_read,
               has_table_privilege(current_user,p.oid,'INSERT') AS private_append,
               has_table_privilege(current_user,p.oid,'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS private_modify,
               has_table_privilege(current_user,q.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE') AS public_access
        FROM pg_roles r
        JOIN pg_namespace n ON n.nspname='executive'
        JOIN pg_class p ON p.relnamespace=n.oid AND p.relname='private_reports'
        JOIN pg_namespace i ON i.nspname='insights'
        JOIN pg_class q ON q.relnamespace=i.oid AND q.relname='public_report_drafts'
        WHERE r.rolname=current_user
    """)).mappings().one())


def _check_roles(public: Engine, private: Engine) -> None:
    with public.connect() as pub, private.connect() as priv:
        a, b = _identity(pub), _identity(priv)
        privileged = ("rolsuper", "rolbypassrls", "rolcreaterole", "rolcreatedb")
        if (a["role"] == b["role"] or a["database"] != b["database"]
                or any(a[k] or b[k] for k in privileged)
                or any(a[k] for k in ("private_usage", "private_create", "private_read", "private_append", "private_modify"))
                or not b["private_usage"] or not b["private_read"] or not b["private_append"]
                or b["private_create"] or b["private_modify"] or b["public_access"]
                or pub.execute(text("SELECT pg_has_role(current_user,:role,'MEMBER')"),
                               {"role": b["role"]}).scalar_one()
                or priv.execute(text("SELECT pg_has_role(current_user,:role,'MEMBER')"),
                                {"role": a["role"]}).scalar_one()):
            raise ExecutiveStorageUnavailable("executive storage role isolation failed")


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _aad(subject: str, report_id: str, slot: int) -> bytes:
    return _json(["private_report", "1.0", subject, report_id, slot])


class ExecutiveReportStore:
    def __init__(self, public_engine: Engine, private_engine: Engine, objects: LocalObjectStore, key: bytes) -> None:
        if (public_engine.dialect.name != "postgresql" or private_engine.dialect.name != "postgresql"
                or len(key) != 32):
            raise ExecutiveStorageUnavailable("invalid executive storage configuration")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        _check_roles(public_engine, private_engine)
        self._public = public_engine
        self._engine = private_engine
        self._objects = objects
        self._cipher = AESGCM(key)

    def close(self) -> None:
        self._engine.dispose()

    def _read(self, subject: str, row: Mapping[str, Any]) -> dict[str, Any]:
        from cryptography.exceptions import InvalidTag

        ref = row["content_ref"]
        digest = row["sha256"]
        if ref != "reports/" + digest + ".bin":
            raise ValueError("private report reference mismatch")
        data = self._objects.get(ref)
        if (data is None or len(data) > _MAX_CONTENT + 128 or not data.startswith(_MAGIC)
                or hashlib.sha256(data).hexdigest() != digest):
            raise ValueError("private report integrity failure")
        start = len(_MAGIC)
        try:
            clear = self._cipher.decrypt(data[start:start+12], data[start+12:],
                                         _aad(subject, row["report_id"], row["slot"]))
            saved = json.loads(clear)
            validate_payload("private_report", saved["payload"])
            if saved["payload"]["report_id"] != row["report_id"] or not isinstance(saved["evidence"], list):
                raise ValueError("private report identity mismatch")
            refs = {item["source_ref"] for item in saved["evidence"]}
            if (refs != set(saved["payload"]["source_refs"]) or len(refs) != len(saved["evidence"])
                    or any(not set(s["source_refs"]) <= refs for s in saved["payload"]["sections"])):
                raise ValueError("private report evidence mismatch")
        except (InvalidTag, ValueError, TypeError, KeyError) as exc:
            raise ValueError("private report integrity failure") from exc
        return dict(saved)

    def save(self, subject: str, slot: int, report: Mapping[str, Any],
             evidence: list[dict[str, Any]]) -> dict[str, Any]:
        validate_payload("private_report", report)
        if not 5 <= slot <= 7 or not 1 <= len(subject) <= 64 or not 1 <= len(report["report_id"]) <= 64:
            raise ValueError("invalid private report identity")
        _check_roles(self._public, self._engine)
        params = {"subject": subject, "id": report["report_id"], "slot": slot}
        with self._engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                         {"key": subject + "/" + report["report_id"]})
            row = conn.execute(text("""
                SELECT report_id,slot,content_ref,sha256 FROM executive.private_reports
                WHERE subject_id=:subject AND report_id=:id
            """), params).mappings().one_or_none()
            if row is not None:
                saved = self._read(subject, dict(row))
                if (row["slot"] != slot
                        or {k: v for k, v in saved["payload"].items() if k != "generated_at"}
                        != {k: v for k, v in report.items() if k != "generated_at"}
                        or saved["evidence"] != evidence):
                    raise ValueError("immutable private report conflict")
                return saved
            saved = {"payload": dict(report), "evidence": evidence}
            clear = _json(saved)
            if len(clear) > _MAX_CONTENT:
                raise ValueError("private report exceeds storage bound")
            nonce = os.urandom(12)
            data = _MAGIC + nonce + self._cipher.encrypt(nonce, clear, _aad(subject, report["report_id"], slot))
            digest = hashlib.sha256(data).hexdigest()
            ref = "reports/" + digest + ".bin"
            if self._objects.put(ref, data) != ref or self._objects.get(ref) != data:
                raise ValueError("private report object verification failed")
            conn.execute(text("""
                INSERT INTO executive.private_reports (subject_id,report_id,slot,content_ref,sha256)
                VALUES (:subject,:id,:slot,:ref,:hash)
            """), {**params, "ref": ref, "hash": digest})
            return saved

    def latest(self, subject: str, slot: int) -> dict[str, Any] | None:
        _check_roles(self._public, self._engine)
        with self._engine.connect() as conn:
            row = conn.execute(text("""
                SELECT report_id,slot,content_ref,sha256 FROM executive.private_reports
                WHERE subject_id=:subject AND slot=:slot
                ORDER BY created_at DESC,report_id DESC LIMIT 1
            """), {"subject": subject, "slot": slot}).mappings().one_or_none()
        return self._read(subject, dict(row)) if row is not None else None


def build_executive_store(public: Engine, public_objects: LocalObjectStore,
                          environ: Mapping[str, str]) -> ExecutiveReportStore | None:
    url, root, encoded = (environ.get(k, "") for k in (
        "ETEMAD_EXECUTIVE_DB_URL", "ETEMAD_EXECUTIVE_OBJECT_ROOT", "ETEMAD_SECRET_EXECUTIVE_REPORTS_KEY"))
    if not any((url, root, encoded)):
        return None
    if not all((url, root, encoded)):
        raise ExecutiveStorageUnavailable("incomplete executive storage configuration")
    private: Engine | None = None
    try:
        private_url = make_url(url)
        ordinary = public.url
        if ((private_url.get_backend_name(), private_url.host, private_url.port, private_url.database,
             private_url.query.get("host"), private_url.query.get("port"))
                != (ordinary.get_backend_name(), ordinary.host, ordinary.port, ordinary.database,
                    ordinary.query.get("host"), ordinary.query.get("port"))):
            raise ExecutiveStorageUnavailable("executive index must use the central database")
        private_root = Path(root).resolve()
        public_root = public_objects.root.resolve()
        if (private_root == public_root or private_root in public_root.parents or public_root in private_root.parents):
            raise ExecutiveStorageUnavailable("executive object root overlaps public storage")
        private_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stat.S_IMODE(private_root.stat().st_mode) & 0o077:
            raise ExecutiveStorageUnavailable("executive object root is not private")
        key = base64.b64decode(encoded, validate=True)
        if len(key) != 32:
            raise ExecutiveStorageUnavailable("invalid executive encryption key")
        private = create_engine(private_url, pool_pre_ping=True, hide_parameters=True)
        return ExecutiveReportStore(public, private, LocalObjectStore(private_root), key)
    except (ValueError, binascii.Error, OSError, ExecutiveStorageUnavailable):
        if private is not None:
            private.dispose()
        raise ExecutiveStorageUnavailable("executive storage configuration rejected") from None
    except Exception:
        if private is not None:
            private.dispose()
        raise ExecutiveStorageUnavailable("executive storage isolation unavailable") from None
