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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analysis_rules import (
    is_finite_scalar,
    normalize_checks,
    normalize_report_requirements,
    values_equal,
)
from .errors import MdlConfigError, MdlPolicyError
from .manifest import resolve_manifest_path
from .models import MdlManifest, normalize_name
from .retrieval import is_exact_match, score_entries
from .service import (
    MdlQueryService,
    _database_snapshot_path,
    _document_database,
    _read_json,
    infer_wren_data_source,
)

_SAVE_LOCK = threading.Lock()
_VALID_KINDS = {"qa_sql", "business_rule", "glossary", "analysis_rule"}
_VALID_STATUSES = {"draft", "approved"}
_WRITE_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|REPLACE|"
    r"GRANT|REVOKE|CALL|COPY|LOAD|VACUUM|ANALYZE)\b",
    re.IGNORECASE,
)
_MAX_EXPECTED_ROWS = 100


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
            prior_by_id = {
                str(uuid.UUID(entry["id"])): entry for entry in current["entries"]
            }
            normalized = []
            seen_ids: set[str] = set()
            for entry in entries:
                item = _normalize_entry(
                    entry,
                    prior=prior_by_id.get(_canonical_entry_id(entry.get("id"))),
                )
                if item["id"] in seen_ids:
                    raise MdlConfigError(
                        f"Duplicate MDL knowledge entry id: {item['id']}."
                    )
                seen_ids.add(item["id"])
                normalized.append(item)
            next_store = {
                "database": database,
                "revision": current["revision"] + 1,
                "entries": normalized,
            }
            _atomic_write_json(path, next_store)
            return self._with_stale_flags(next_store, database)

    def approve(
        self,
        database: str,
        entry_id: str,
        revision: int,
        *,
        connector: Any | None = None,
        data_source: str | None = None,
        connection_info: dict[str, Any] | None = None,
        reviewed_by: str = "admin",
    ) -> dict[str, Any]:
        path = resolve_knowledge_path(self._manifest_path, database)
        entry_id = _canonical_entry_id(entry_id) or entry_id
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
                    expected_result = entry.get("expected_result")
                    validation_mode = "wren_plan_explain"
                    if expected_result is not None:
                        if connector is None:
                            raise MdlPolicyError(
                                "Expected SQL results require a datasource connector."
                            )
                        columns, rows = connector.query_ex(
                            "SELECT * FROM ("
                            + planned.rstrip().rstrip(";")
                            + f") AS mdl_knowledge_check LIMIT {_MAX_EXPECTED_ROWS + 1}"
                        )
                        _compare_expected_result(expected_result, columns, rows)
                        validation_mode = "result_checked"
                    elif connector is not None:
                        connector.query_ex("EXPLAIN " + planned)
                    entry["validation"] = {
                        "mode": validation_mode,
                        "data_source": data_source
                        or _infer_data_source(connector)
                        or "duckdb",
                        "reviewed_by": reviewed_by,
                        "validated_at": datetime.now(timezone.utc).isoformat(),
                    }
                entry["status"] = "approved"
                entry["mdl_fingerprint"] = fingerprint
                entries[index] = entry
                next_store = {
                    "database": database,
                    "revision": store["revision"] + 1,
                    "entries": entries,
                }
                _atomic_write_json(path, next_store)
                return self._with_stale_flags(next_store, database)
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
        for entry in score_entries(self.load(database)["entries"], question):
            item = _bounded_entry(entry)
            item["score"] = entry["score"]
            item["exact_match"] = entry["exact_match"]
            item["stale"] = (
                item.get("status") == "approved"
                and item.get("mdl_fingerprint") != fingerprint
            )
            scored.append(item)
        return _top_matches(_mark_conflicts(scored), top_k)

    def search(
        self,
        database: str,
        question: str,
        user_id: str | None,
        role: str | None,
        top_k: int | None = 4,
        data_source: str | None = None,
        connection_info: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        document = self._load_database_document(database)
        fingerprint = manifest_fingerprint(document)
        matches = []
        eligible = []
        for entry in self.load(database)["entries"]:
            if entry.get("status") != "approved":
                continue
            if entry.get("mdl_fingerprint") != fingerprint:
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
            eligible.append(entry)
        for entry in score_entries(eligible, question):
            item = _bounded_entry(entry)
            item["score"] = entry["score"]
            item["exact_match"] = entry["exact_match"]
            item["stale"] = False
            matches.append(item)
        ranked = _top_matches(_mark_conflicts(matches), top_k)
        return [_runtime_entry(match) for match in ranked]

    def resolve_exact_sql(
        self,
        database: str,
        question: str,
        user_id: str | None,
        role: str | None,
        **kwargs: Any,
    ) -> str | None:
        entry = self.resolve_exact_entry(database, question, user_id, role, **kwargs)
        return entry["sql"] if entry else None

    def resolve_exact_entry(
        self,
        database: str,
        question: str,
        user_id: str | None,
        role: str | None,
        *,
        data_source: str | None = None,
        connection_info: dict[str, Any] | None = None,
        entry_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return an auditable reviewed entry for an explicitly enabled match."""
        entry_id = _canonical_entry_id(entry_id)
        document = self._load_database_document(database)
        fingerprint = manifest_fingerprint(document)
        exact_entries = []
        candidates = []
        selected = None
        for entry in self.load(database)["entries"]:
            if entry_id is not None and entry.get("id") == entry_id:
                selected = entry
            if (
                entry.get("kind") != "qa_sql"
                or entry.get("status") != "approved"
                or entry.get("mdl_fingerprint") != fingerprint
                or not is_exact_match(entry, question)
                or not self._bindings_authorized(entry, document, user_id, role)
            ):
                continue
            exact_entries.append(entry)
            if not entry.get("reuse_on_exact_match"):
                continue
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
            candidates.append(entry)
        if len({_sql_key(entry["sql"]) for entry in exact_entries}) > 1:
            raise MdlPolicyError(
                "Exact question matches multiple reviewed SQL definitions "
                "with different semantics."
            )
        if entry_id is not None and selected not in candidates:
            raise MdlPolicyError(
                "The requested knowledge SQL is not an authorized exact match."
            )
        if not candidates:
            return None
        return _runtime_entry(candidates[0])

    def _with_stale_flags(self, store: dict[str, Any], database: str) -> dict[str, Any]:
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
        return {**store, "entries": entries}

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
            _normalize_entry(entry, preserve_approved=True) for entry in entries
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
        entry_id = str(uuid.UUID(entry_id))
    except ValueError as exc:
        raise MdlConfigError("MDL knowledge entry id must be a UUID string.") from exc
    entry_kind = str(raw.get("kind", ""))
    if entry_kind not in _VALID_KINDS:
        raise MdlConfigError(
            "MDL knowledge entry kind must be qa_sql, business_rule, glossary "
            "or analysis_rule."
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
    reuse_on_exact_match = bool(raw.get("reuse_on_exact_match", False))
    if entry_kind != "qa_sql" and reuse_on_exact_match:
        raise MdlConfigError("reuse_on_exact_match is only valid for qa_sql knowledge.")
    expected_result = _normalize_expected_result(raw.get("expected_result"))
    if entry_kind != "qa_sql" and expected_result is not None:
        raise MdlConfigError("expected_result is only valid for qa_sql knowledge.")
    terms = _normalize_terms(raw.get("terms"))
    rule = _normalize_rule(raw.get("rule"))
    if entry_kind == "glossary" and not terms:
        raise MdlConfigError("Glossary knowledge requires at least one term.")
    if entry_kind == "analysis_rule" and not rule:
        raise MdlConfigError("Analysis rule knowledge requires a rule object.")
    if entry_kind != "glossary" and terms:
        raise MdlConfigError("Glossary terms are only valid for glossary knowledge.")
    if entry_kind != "analysis_rule" and rule:
        raise MdlConfigError(
            "Analysis rules are only valid for analysis_rule knowledge."
        )
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
        "reuse_on_exact_match": reuse_on_exact_match,
        "expected_result": expected_result,
        "aliases": [alias.strip() for alias in aliases if alias.strip()],
        "models": models,
        "terms": terms,
        "rule": rule,
        "status": "draft",
    }
    fingerprint = raw.get("mdl_fingerprint")
    audit_same = prior is not None and _audit_payload(prior) == _audit_payload(
        normalized
    )
    if preserve_approved and status == "approved" and fingerprint:
        normalized["status"] = "approved"
        normalized["mdl_fingerprint"] = str(fingerprint)
        if isinstance(raw.get("validation"), dict):
            normalized["validation"] = deepcopy(raw["validation"])
    elif (
        prior is not None
        and prior.get("status") == "approved"
        and audit_same
        and prior.get("mdl_fingerprint")
    ):
        normalized["status"] = "approved"
        normalized["mdl_fingerprint"] = prior["mdl_fingerprint"]
        if prior.get("validation") is not None:
            normalized["validation"] = deepcopy(prior["validation"])
    return normalized


def _canonical_entry_id(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError):
        return str(value)


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
                isinstance(column, str) and column.strip() for column in raw_columns
            )
        ):
            raise MdlConfigError(
                "MDL knowledge model bindings must be non-empty string lists."
            )
        result[model] = [column.strip() for column in raw_columns]
    return result


def _normalize_terms(raw_terms: Any) -> list[dict[str, Any]]:
    if raw_terms in (None, ""):
        return []
    if not isinstance(raw_terms, list):
        raise MdlConfigError("MDL glossary terms must be a list.")
    terms = []
    for raw_term in raw_terms:
        if not isinstance(raw_term, dict):
            raise MdlConfigError("Each glossary term must be an object.")
        term = _required_string(raw_term, "term")
        meaning = _required_string(raw_term, "meaning")
        aliases = raw_term.get("aliases", [])
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) and alias.strip() for alias in aliases
        ):
            raise MdlConfigError("Glossary term aliases must be a string list.")
        terms.append(
            {
                "term": term,
                "meaning": meaning,
                "aliases": [alias.strip() for alias in aliases],
            }
        )
    return terms


def _normalize_rule(raw_rule: Any) -> dict[str, Any]:
    if raw_rule in (None, ""):
        return {}
    if not isinstance(raw_rule, dict):
        raise MdlConfigError("Analysis rule must be an object.")
    if not raw_rule:
        return {}
    metric = _required_string(raw_rule, "metric")
    conditions = raw_rule.get("conditions", [])
    if not isinstance(conditions, list) or not all(
        isinstance(condition, str) and condition.strip() for condition in conditions
    ):
        raise MdlConfigError("Analysis rule conditions must be a string list.")
    guidance = str(raw_rule.get("guidance") or "").strip()
    checks = normalize_checks(raw_rule.get("checks"))
    report_requirements = normalize_report_requirements(
        raw_rule.get("report_requirements")
    )
    return {
        "metric": metric,
        "conditions": [condition.strip() for condition in conditions],
        "guidance": guidance,
        "checks": checks,
        "report_requirements": report_requirements,
    }


def _normalize_expected_result(raw_result: Any) -> dict[str, Any] | None:
    if raw_result in (None, ""):
        return None
    if not isinstance(raw_result, dict):
        raise MdlConfigError("Expected SQL result must be an object.")
    columns = raw_result.get("columns")
    rows = raw_result.get("rows")
    if (
        not isinstance(columns, list)
        or not all(isinstance(column, str) and column.strip() for column in columns)
        or not isinstance(rows, list)
        or len(rows) > _MAX_EXPECTED_ROWS
    ):
        raise MdlConfigError(
            "Expected SQL result requires columns and at most 100 rows."
        )
    normalized_rows = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise MdlConfigError(
                "Expected SQL result rows must match the column count."
            )
        if not all(_is_scalar(value) for value in row):
            raise MdlConfigError("Expected SQL result rows must contain scalar values.")
        normalized_rows.append(deepcopy(row))
    return {
        "columns": [column.strip() for column in columns],
        "rows": normalized_rows,
    }


def _is_scalar(value: Any) -> bool:
    return is_finite_scalar(value)


def _compare_expected_result(
    expected: dict[str, Any], columns: list[Any], rows: list[Any]
) -> None:
    actual_columns = [
        str(column[0]) if isinstance(column, tuple) else str(column)
        for column in columns
    ]
    actual_rows = [list(row) for row in rows]
    if actual_columns != expected["columns"] or len(actual_rows) > _MAX_EXPECTED_ROWS:
        raise MdlConfigError("Validated SQL result does not match the expected result.")
    if len(actual_rows) != len(expected["rows"]):
        raise MdlConfigError("Validated SQL result does not match the expected result.")
    for actual_row, expected_row in zip(actual_rows, expected["rows"]):
        if len(actual_row) != len(expected_row) or any(
            not _values_equal(actual, wanted)
            for actual, wanted in zip(actual_row, expected_row)
        ):
            raise MdlConfigError(
                "Validated SQL result does not match the expected result."
            )


def _values_equal(actual: Any, expected: Any) -> bool:
    return values_equal(actual, expected)


def _audit_payload(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(entry.get(key))
        for key in (
            "kind",
            "title",
            "question",
            "content",
            "sql",
            "reuse_on_exact_match",
            "expected_result",
            "aliases",
            "models",
            "terms",
            "rule",
        )
    }


def _sql_key(sql: str) -> str:
    import sqlglot

    try:
        return sqlglot.parse_one(sql, read="duckdb").sql(dialect="duckdb")
    except Exception:
        return sql.strip().rstrip(";")


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
            "reuse_on_exact_match",
            "aliases",
            "models",
            "status",
            "mdl_fingerprint",
            "terms",
            "rule",
            "validation",
            "expected_result",
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
        "reuse_on_exact_match": bool(entry.get("reuse_on_exact_match", False)),
        "aliases": deepcopy(entry.get("aliases", [])),
        "expected_result": deepcopy(entry.get("expected_result")),
        "models": deepcopy(entry.get("models", {})),
        "terms": deepcopy(entry.get("terms", [])),
        "rule": deepcopy(entry.get("rule", {})),
        "validation": deepcopy(entry.get("validation")),
        "exact_match": bool(entry.get("exact_match", False)),
        "conflict": bool(entry.get("conflict", False)),
    }


def _top_matches(
    matches: list[dict[str, Any]], top_k: int | None
) -> list[dict[str, Any]]:
    limit = len(matches) if top_k is None else max(1, min(int(top_k or 4), 20))
    return sorted(
        matches,
        key=lambda item: (bool(item.get("exact_match")), item["score"]),
        reverse=True,
    )[:limit]


def _mark_conflicts(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    exact_sqls = {
        _sql_key(str(item.get("sql", "")))
        for item in matches
        if item.get("kind") == "qa_sql" and item.get("exact_match")
    }
    conflict = len(exact_sqls) > 1
    for item in matches:
        item["conflict"] = bool(conflict and item.get("exact_match"))
    return matches


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
