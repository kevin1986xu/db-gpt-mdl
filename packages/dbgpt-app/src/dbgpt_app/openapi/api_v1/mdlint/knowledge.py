"""Datasource-scoped MDL knowledge entries for prompt grounding."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from .errors import MdlConfigError, MdlPolicyError
from .manifest import resolve_manifest_path
from .models import MdlManifest, normalize_name
from .service import (
    MdlQueryService,
    _database_snapshot_path,
    _document_database,
    _read_json,
    infer_wren_data_source,
)

_SAVE_LOCK = threading.Lock()
_VALID_KINDS = {"qa_sql", "business_rule"}
_VALID_STATUSES = {"draft", "approved"}
_WRITE_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|REPLACE|"
    r"GRANT|REVOKE|CALL|COPY|LOAD|VACUUM|ANALYZE)\b",
    re.IGNORECASE,
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_CHINESE_CHUNK = re.compile(r"[\u4e00-\u9fff]+")
_LOW_SIGNAL_TOKENS = {
    "查询",
    "统计",
    "数量",
    "多少",
    "量是",
    "是多",
    "几个",
    "多少个",
    "一下",
}


def manifest_fingerprint(manifest_dict: dict[str, Any]) -> str:
    """Return a stable digest for MDL semantics, excluding policies/revision."""
    manifest = manifest_dict.get("manifest", manifest_dict)
    if not isinstance(manifest, dict):
        raise MdlConfigError("MDL manifest must be a JSON object.")
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_knowledge_path(
    manifest_path: str | Path | None,
    database: str,
) -> Path:
    digest = hashlib.sha256(database.encode("utf-8")).hexdigest()
    return resolve_manifest_path(manifest_path).with_name("mdl.knowledge") / (
        f"{digest}.json"
    )


class KnowledgeService:
    """Manage reviewed Q&A SQL and business rules bound to an MDL datasource."""

    def __init__(
        self,
        manifest_path: str | Path | None = None,
        query_service_factory: type[MdlQueryService] = MdlQueryService,
    ):
        self._manifest_path = manifest_path
        self._query_service_factory = query_service_factory

    def load(self, database: str, *, include_stale: bool = False) -> dict[str, Any]:
        path = resolve_knowledge_path(self._manifest_path, database)
        store = _read_store(path, database)
        if include_stale:
            document = self._load_database_document(database)
            fingerprint = manifest_fingerprint(document)
            entries = []
            for entry in store["entries"]:
                item = deepcopy(entry)
                item["stale"] = (
                    item.get("status") == "approved"
                    and item.get("mdl_fingerprint") != fingerprint
                )
                entries.append(item)
            store = {**store, "entries": entries}
        return store

    def save(
        self,
        database: str,
        entries: list[dict[str, Any]],
        revision: int,
    ) -> dict[str, Any]:
        path = resolve_knowledge_path(self._manifest_path, database)
        with _SAVE_LOCK:
            current = _read_store(path, database)
            if revision != current["revision"]:
                raise MdlConfigError(
                    "MDL knowledge revision conflict: "
                    f"expected {current['revision']}, got {revision}."
                )
            prior_by_id = {entry["id"]: entry for entry in current["entries"]}
            normalized = [
                _normalize_entry(entry, prior=prior_by_id.get(str(entry.get("id"))))
                for entry in entries
            ]
            next_store = {
                "database": database,
                "revision": current["revision"] + 1,
                "entries": normalized,
            }
            _atomic_write_json(path, next_store)
            return next_store

    def approve(
        self,
        database: str,
        entry_id: str,
        revision: int,
        *,
        connector: Any | None = None,
        data_source: str | None = None,
        connection_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = resolve_knowledge_path(self._manifest_path, database)
        with _SAVE_LOCK:
            store = _read_store(path, database)
            if revision != store["revision"]:
                raise MdlConfigError(
                    "MDL knowledge revision conflict: "
                    f"expected {store['revision']}, got {revision}."
                )
            document = self._load_database_document(database)
            fingerprint = manifest_fingerprint(document)
            entries = deepcopy(store["entries"])
            for index, raw_entry in enumerate(entries):
                if raw_entry["id"] != entry_id:
                    continue
                entry = _normalize_entry(raw_entry)
                self._validate_bindings(entry, document)
                if entry["kind"] == "qa_sql":
                    self._validate_readonly_sql(entry["sql"])
                    planned = self._plan_sql(
                        database,
                        entry["sql"],
                        user_id="admin",
                        role="admin",
                        data_source=data_source
                        or _infer_data_source(connector)
                        or "duckdb",
                        connection_info=connection_info,
                    )
                    if connector is not None:
                        connector.query_ex("EXPLAIN " + planned)
                entry["status"] = "approved"
                entry["mdl_fingerprint"] = fingerprint
                entries[index] = entry
                next_store = {
                    "database": database,
                    "revision": store["revision"] + 1,
                    "entries": entries,
                }
                _atomic_write_json(path, next_store)
                return next_store
            raise MdlConfigError(f"MDL knowledge entry not found: {entry_id}.")

    def preview_search(
        self,
        database: str,
        question: str,
        *,
        top_k: int = 4,
    ) -> list[dict[str, Any]]:
        document = self._load_database_document(database)
        fingerprint = manifest_fingerprint(document)
        scored = []
        for entry in self.load(database)["entries"]:
            score = _score_entry(entry, question)
            if score <= 0:
                continue
            item = _bounded_entry(entry)
            item["score"] = score
            item["stale"] = (
                item.get("status") == "approved"
                and item.get("mdl_fingerprint") != fingerprint
            )
            scored.append(item)
        return _top_matches(scored, top_k)

    def search(
        self,
        database: str,
        question: str,
        user_id: str | None,
        role: str | None,
        top_k: int = 4,
        data_source: str | None = None,
        connection_info: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        document = self._load_database_document(database)
        fingerprint = manifest_fingerprint(document)
        matches = []
        for entry in self.load(database)["entries"]:
            if entry.get("status") != "approved":
                continue
            if entry.get("mdl_fingerprint") != fingerprint:
                continue
            score = _score_entry(entry, question)
            if score <= 0:
                continue
            if not self._bindings_authorized(entry, document, user_id, role):
                continue
            if entry["kind"] == "qa_sql":
                try:
                    self._plan_sql(
                        database,
                        entry["sql"],
                        user_id=user_id,
                        role=role,
                        data_source=data_source or "duckdb",
                        connection_info=connection_info,
                    )
                except Exception:
                    continue
            item = _bounded_entry(entry)
            item["score"] = score
            item["stale"] = False
            matches.append(item)
        return [_runtime_entry(match) for match in _top_matches(matches, top_k)]

    def _load_database_document(self, database: str) -> dict[str, Any]:
        return MdlQueryService(manifest_path=self._manifest_path).load(database)

    def _plan_sql(
        self,
        database: str,
        sql: str,
        *,
        user_id: str | None,
        role: str | None,
        data_source: str,
        connection_info: dict[str, Any] | None = None,
    ) -> str:
        service = self._query_service_factory(
            manifest_path=_manifest_path_for_database(self._manifest_path, database)
        )
        return service.plan_authorized_sql(
            sql,
            user_id=user_id,
            role=role,
            data_source=data_source,
            connection_info=connection_info,
        ).planned_sql

    def _validate_bindings(
        self,
        entry: dict[str, Any],
        document: dict[str, Any],
    ) -> None:
        manifest = MdlManifest.from_dict(document)
        models = entry["models"]
        if entry["kind"] == "business_rule" and not models:
            raise MdlConfigError("Business rules must bind at least one MDL field.")
        for raw_model, raw_columns in models.items():
            model_name = normalize_name(raw_model)
            if model_name not in manifest.models:
                raise MdlConfigError(
                    f"Knowledge entry references unknown MDL model: {raw_model}."
                )
            columns = {normalize_name(column) for column in raw_columns}
            unknown = columns - manifest.models[model_name].columns
            if unknown:
                raise MdlConfigError(
                    f"Knowledge entry references unknown columns for {raw_model}: "
                    f"{', '.join(sorted(unknown))}."
                )

    def _bindings_authorized(
        self,
        entry: dict[str, Any],
        document: dict[str, Any],
        user_id: str | None,
        role: str | None,
    ) -> bool:
        manifest = MdlManifest.from_dict(document)
        effective = manifest.effective_manifest(user_id, role=role)
        allowed = {
            normalize_name(model["name"]): {
                normalize_name(column["name"])
                for column in model.get("columns", [])
                if isinstance(column, dict) and column.get("name")
            }
            for model in effective.get("models", [])
        }
        for raw_model, raw_columns in entry["models"].items():
            model_name = normalize_name(raw_model)
            columns = {normalize_name(column) for column in raw_columns}
            if model_name not in allowed or not columns <= allowed[model_name]:
                return False
        return True

    def _validate_readonly_sql(self, sql: str) -> None:
        if _WRITE_SQL.search(sql):
            raise MdlPolicyError("Knowledge QA SQL must be read-only.")
        try:
            import sqlglot
            from sqlglot import exp

            expressions = sqlglot.parse(sql, read="duckdb")
        except Exception as exc:
            raise MdlConfigError(f"Knowledge QA SQL is invalid: {exc}") from exc
        if len(expressions) != 1 or expressions[0] is None:
            raise MdlPolicyError("Knowledge QA SQL must contain one statement.")
        if not isinstance(
            expressions[0],
            (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery),
        ):
            raise MdlPolicyError("Knowledge QA SQL must be a SELECT query.")


def _manifest_path_for_database(
    manifest_path: str | Path | None,
    database: str,
) -> Path:
    path = resolve_manifest_path(manifest_path)
    if path.exists():
        active = _read_json(path)
        if _document_database(active) == database:
            return path
    snapshot_path = _database_snapshot_path(path, database)
    if snapshot_path.exists():
        return snapshot_path
    return path


def _infer_data_source(connector: Any | None) -> str | None:
    if connector is None:
        return None
    try:
        return infer_wren_data_source(connector)
    except Exception:
        return None


def _read_store(path: Path, database: str) -> dict[str, Any]:
    if not path.exists():
        return {"database": database, "revision": 0, "entries": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MdlConfigError(f"MDL knowledge store is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise MdlConfigError(f"Failed to read MDL knowledge store: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("database") != database:
        raise MdlConfigError("MDL knowledge store database mismatch.")
    entries = raw.get("entries")
    if not isinstance(entries, list):
        raise MdlConfigError("MDL knowledge entries must be a list.")
    return {
        "database": database,
        "revision": int(raw.get("revision", 0) or 0),
        "entries": [
            _normalize_entry(entry, preserve_approved=True)
            for entry in entries
        ],
    }


def _normalize_entry(
    raw: dict[str, Any],
    *,
    prior: dict[str, Any] | None = None,
    preserve_approved: bool = False,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MdlConfigError("MDL knowledge entry must be a JSON object.")
    entry_id = str(raw.get("id") or uuid.uuid4())
    try:
        uuid.UUID(entry_id)
    except ValueError as exc:
        raise MdlConfigError("MDL knowledge entry id must be a UUID string.") from exc
    entry_kind = str(raw.get("kind", ""))
    if entry_kind not in _VALID_KINDS:
        raise MdlConfigError(
            "MDL knowledge entry kind must be qa_sql or business_rule."
        )
    title = _required_string(raw, "title")
    content = _required_string(raw, "content")
    question = str(raw.get("question") or "").strip()
    sql = str(raw.get("sql") or "").strip()
    if entry_kind == "qa_sql":
        if not question:
            raise MdlConfigError("QA SQL knowledge requires question.")
        if not sql:
            raise MdlConfigError("QA SQL knowledge requires sql.")
    elif question or sql:
        sql = ""
    aliases = raw.get("aliases", [])
    if not isinstance(aliases, list) or not all(
        isinstance(item, str) for item in aliases
    ):
        raise MdlConfigError("MDL knowledge aliases must be a string list.")
    models = _normalize_models(raw.get("models"))
    status = str(raw.get("status", "draft"))
    if status not in _VALID_STATUSES:
        raise MdlConfigError("MDL knowledge status must be draft or approved.")
    normalized = {
        "id": entry_id,
        "kind": entry_kind,
        "title": title,
        "question": question,
        "content": content,
        "sql": sql,
        "aliases": [alias.strip() for alias in aliases if alias.strip()],
        "models": models,
        "status": "draft",
    }
    fingerprint = raw.get("mdl_fingerprint")
    audit_same = (
        prior is not None
        and _audit_payload(prior) == _audit_payload(normalized)
    )
    if preserve_approved and status == "approved" and fingerprint:
        normalized["status"] = "approved"
        normalized["mdl_fingerprint"] = str(fingerprint)
    elif (
        prior is not None
        and prior.get("status") == "approved"
        and audit_same
        and prior.get("mdl_fingerprint")
    ):
        normalized["status"] = "approved"
        normalized["mdl_fingerprint"] = prior["mdl_fingerprint"]
    return normalized


def _required_string(raw: dict[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MdlConfigError(f"MDL knowledge entry requires {field}.")
    return value.strip()


def _normalize_models(raw_models: Any) -> dict[str, list[str]]:
    if not isinstance(raw_models, dict) or not raw_models:
        raise MdlConfigError("MDL knowledge entry must bind at least one model field.")
    result: dict[str, list[str]] = {}
    for raw_model, raw_columns in raw_models.items():
        model = str(raw_model).strip()
        if not model:
            raise MdlConfigError("MDL knowledge model name cannot be empty.")
        if (
            not isinstance(raw_columns, list)
            or not raw_columns
            or not all(
                isinstance(column, str) and column.strip()
                for column in raw_columns
            )
        ):
            raise MdlConfigError(
                "MDL knowledge model bindings must be non-empty string lists."
            )
        result[model] = [column.strip() for column in raw_columns]
    return result


def _audit_payload(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(entry.get(key))
        for key in (
            "kind",
            "title",
            "question",
            "content",
            "sql",
            "aliases",
            "models",
        )
    }


def _score_entry(entry: dict[str, Any], question: str) -> float:
    query_tokens = _tokens(question)
    if not query_tokens:
        return 0
    text_parts = [
        entry.get("title", ""),
        entry.get("question", ""),
        entry.get("content", ""),
        entry.get("sql", ""),
        " ".join(entry.get("aliases", [])),
        " ".join(entry.get("models", {}).keys()),
        " ".join(
            column
            for columns in entry.get("models", {}).values()
            for column in columns
        ),
    ]
    entry_tokens = _tokens(" ".join(text_parts))
    overlap = query_tokens & entry_tokens
    if not overlap or not (overlap - _LOW_SIGNAL_TOKENS):
        return 0
    score = float(len(overlap))
    aliases = {alias.lower() for alias in entry.get("aliases", [])}
    lowered = question.lower()
    score += sum(2.0 for alias in aliases if alias and alias in lowered)
    if entry.get("question") and entry["question"].strip() == question.strip():
        score += 4.0
    return score


def _tokens(text: str) -> set[str]:
    lowered = text.lower()
    tokens = set(_IDENTIFIER.findall(lowered))
    for chunk in _CHINESE_CHUNK.findall(lowered):
        tokens.add(chunk)
        tokens.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return {token for token in tokens if token}


def _bounded_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(entry.get(key))
        for key in (
            "id",
            "kind",
            "title",
            "question",
            "content",
            "sql",
            "aliases",
            "models",
            "status",
            "mdl_fingerprint",
        )
        if key in entry
    }


def _runtime_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": entry.get("status", "approved"),
        "stale": bool(entry.get("stale", False)),
        "id": entry["id"],
        "title": entry["title"],
        "kind": entry["kind"],
        "question": entry.get("question", ""),
        "content": entry.get("content", ""),
        "sql": entry.get("sql", ""),
        "models": deepcopy(entry.get("models", {})),
    }


def _top_matches(matches: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    limit = max(1, min(int(top_k or 4), 20))
    return sorted(matches, key=lambda item: item["score"], reverse=True)[:limit]


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
