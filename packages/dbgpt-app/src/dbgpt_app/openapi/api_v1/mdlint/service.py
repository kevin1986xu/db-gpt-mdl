"""Service boundary for MDL-planned and policy-authorized SQL execution."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .errors import MdlConfigError, MdlWrenUnavailableError
from .manifest import load_manifest, resolve_manifest_path
from .models import MdlManifest
from .wren_adapter import WrenPlanner

_SAVE_LOCK = threading.Lock()
MDL_MAX_RESULT_ROWS = 1001
MDL_QUERY_TIMEOUT_SECONDS = 30.0


class SqlConnector(Protocol):
    def query_ex(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        fetch: str = "all",
        timeout: float | None = None,
    ):
        """Execute SQL against a DB-GPT connector."""


class SqlPlanner(Protocol):
    def dry_plan(
        self,
        sql: str,
        manifest: MdlManifest,
        data_source: str,
        connection_info: dict[str, Any] | None = None,
    ) -> str:
        """Return target-dialect SQL for an authorized semantic query."""


@dataclass(frozen=True)
class AuthorizedSqlPlan:
    input_sql: str
    policy_sql: str
    planned_sql: str
    referenced_models: frozenset[str] = field(default_factory=frozenset)
    row_filters: dict[str, str] = field(default_factory=dict)
    effective_manifest: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MdlQueryResult:
    plan: AuthorizedSqlPlan
    rows: list


class MdlQueryService:
    """Load MDL, apply policy, plan through Wren, and execute through DB-GPT."""

    def __init__(
        self,
        manifest_path: str | Path | None = None,
        planner: SqlPlanner | None = None,
    ):
        self._manifest_path = manifest_path
        self._planner = planner or WrenPlanner()

    @classmethod
    def from_env(cls, planner: SqlPlanner | None = None) -> "MdlQueryService":
        return cls(planner=planner)

    def load(self, database: str | None = None) -> dict[str, Any]:
        if database is None:
            return self.get_manifest().to_document()
        return _load_database_document(
            resolve_manifest_path(self._manifest_path),
            database,
        )

    def get_manifest(self) -> MdlManifest:
        return load_manifest(self._manifest_path)

    def get(self) -> dict[str, Any]:
        return self.get_manifest().raw

    def get_schema(
        self,
        user_id: str | None,
        role: str | None = None,
    ) -> dict[str, list[dict[str, str]]]:
        return self.get_manifest().schema_for(user_id, role=role)

    def schema(
        self,
        user_id: str | None,
        role: str | None = None,
    ) -> dict[str, list[dict[str, str]]]:
        return self.get_schema(user_id, role=role)

    def validate_manifest(self) -> MdlManifest:
        return self.get_manifest()

    def validate(self, manifest: dict[str, Any] | None = None) -> MdlManifest:
        if manifest is not None:
            return MdlManifest.from_dict(manifest)
        return self.validate_manifest()

    def save_manifest(
        self,
        manifest: dict[str, Any],
        revision: int | None = None,
        expected_active_database: str | None = None,
    ) -> MdlManifest:
        path = resolve_manifest_path(self._manifest_path)
        database = str(manifest.get("database", "mdl_demo"))
        with _SAVE_LOCK:
            if expected_active_database is not None:
                active_database = (
                    _document_database(_read_json(path)) if path.exists() else None
                )
                if active_database != expected_active_database:
                    raise MdlConfigError(
                        "MDL active database conflict: "
                        f"expected {expected_active_database}, got {active_database}."
                    )
            current_revision = _current_database_revision(path, database)
            if (
                revision is not None
                and revision != current_revision
            ):
                raise MdlConfigError(
                    "MDL revision conflict: "
                    f"expected {current_revision}, got {revision}."
                )

            next_revision = current_revision + 1
            parsed = MdlManifest.from_dict(
                {
                    "manifest": manifest.get("manifest", manifest),
                    "policies": manifest.get("policies", []),
                    "revision": next_revision,
                    "database": database,
                }
            )
            document = parsed.to_document()
            if path.exists():
                active = _read_json(path)
                if _document_database(active) != database:
                    _save_active_database_snapshot(path)
            _atomic_write_json(_database_snapshot_path(path, database), document)
            _atomic_write_json(path, document)
            return parsed

    def save(
        self,
        manifest: dict[str, Any],
        revision: int | None = None,
        expected_active_database: str | None = None,
    ) -> MdlManifest:
        return self.save_manifest(
            manifest,
            revision=revision,
            expected_active_database=expected_active_database,
        )

    def get_policies(self) -> list[dict[str, Any]]:
        return self.get_manifest().raw_policies

    def policies(self) -> list[dict[str, Any]]:
        return self.get_policies()

    def save_policies(
        self,
        policies: list[dict[str, Any]],
        revision: int | None = None,
        database: str | None = None,
    ) -> MdlManifest:
        document = self.load(database)
        document["policies"] = policies
        return self.save_manifest(
            document,
            revision=revision,
            expected_active_database=database,
        )

    def effective_manifest(
        self,
        user_id: str | None,
        role: str | None = None,
    ) -> dict[str, Any]:
        return self.get_manifest().effective_manifest(user_id, role=role)

    def map_data_source(self, connector: Any) -> str:
        return infer_wren_data_source(connector)

    def plan_authorized_sql(
        self,
        sql: str,
        user_id: str | None,
        data_source: str,
        connection_info: dict[str, Any] | None = None,
        role: str | None = None,
    ) -> AuthorizedSqlPlan:
        manifest = load_manifest(self._manifest_path)
        effective = manifest.effective_manifest(user_id, role=role)
        effective_manifest = MdlManifest.from_dict(
            {
                "manifest": effective,
                "policies": [],
                "revision": manifest.revision,
                "database": manifest.database,
            }
        )
        planned_sql = self._planner.dry_plan(
            sql,
            effective_manifest,
            data_source,
            connection_info=connection_info,
        )
        return AuthorizedSqlPlan(
            input_sql=sql.strip().rstrip(";"),
            policy_sql=sql.strip().rstrip(";"),
            planned_sql=planned_sql.strip().rstrip(";"),
            referenced_models=frozenset(
                model["name"] for model in effective.get("models", [])
            ),
            effective_manifest=effective,
        )

    def run_authorized_sql(
        self,
        sql: str,
        connector: SqlConnector,
        user_id: str | None,
        data_source: str | None = None,
        connection_info: dict[str, Any] | None = None,
        fetch: str = "all",
    ) -> MdlQueryResult:
        resolved_data_source = data_source or infer_wren_data_source(connector)
        plan = self.plan_authorized_sql(
            sql,
            user_id=user_id,
            data_source=resolved_data_source,
            connection_info=connection_info,
        )
        if not hasattr(connector, "query_ex"):
            raise MdlConfigError("MDL execution requires connector.query_ex().")
        columns, values = connector.query_ex(
            bounded_query_sql(plan.planned_sql),
            fetch=fetch,
            timeout=MDL_QUERY_TIMEOUT_SECONDS,
        )
        rows = [tuple(columns)] + list(values or [])
        return MdlQueryResult(plan=plan, rows=rows)


def infer_wren_data_source(connector: Any) -> str:
    dialect = getattr(connector, "dialect", None) or getattr(connector, "db_type", None)
    if not dialect:
        raise MdlConfigError(
            "Cannot infer Wren data source from connector. Pass data_source "
            "explicitly."
        )
    normalized = str(dialect).lower()
    mapping = {
        "postgresql": "postgres",
        "postgres": "postgres",
        "mysql": "mysql",
        "duckdb": "duckdb",
        "sqlite": "duckdb",
        "clickhouse": "clickhouse",
        "mssql": "mssql",
        "sqlserver": "mssql",
        "oracle": "oracle",
        "trino": "trino",
        "snowflake": "snowflake",
        "bigquery": "bigquery",
    }
    if normalized not in mapping:
        raise MdlWrenUnavailableError(
            f"Wren data source cannot be inferred for DB-GPT dialect {dialect!r}."
        )
    return mapping[normalized]


MDLService = MdlQueryService


def get_service() -> MdlQueryService:
    return MdlQueryService.from_env()


def bounded_query_sql(sql: str, limit: int = MDL_MAX_RESULT_ROWS) -> str:
    return f"SELECT * FROM ({sql.strip().rstrip(';')}) AS mdl_result LIMIT {limit}"


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


def _database_snapshot_path(manifest_path: Path, database: str) -> Path:
    digest = hashlib.sha256(database.encode("utf-8")).hexdigest()
    return manifest_path.with_name(f"{manifest_path.stem}.databases") / f"{digest}.json"


def _empty_database_document(database: str) -> dict[str, Any]:
    return {
        "manifest": {
            "catalog": "wren",
            "schema": "public",
            "models": [],
            "relationships": [],
        },
        "policies": [],
        "revision": 0,
        "database": database,
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MdlConfigError(f"MDL manifest is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise MdlConfigError(f"Failed to read MDL manifest: {exc}") from exc
    if not isinstance(raw, dict):
        raise MdlConfigError("MDL manifest must be a JSON object.")
    return raw


def _document_has_models(document: dict[str, Any]) -> bool:
    manifest = document.get("manifest", document)
    return isinstance(manifest, dict) and bool(manifest.get("models"))


def _document_database(document: dict[str, Any]) -> str:
    return str(document.get("database", "mdl_demo"))


def _document_to_api_response(document: dict[str, Any]) -> dict[str, Any]:
    if not _document_has_models(document):
        database = _document_database(document)
        result = _empty_database_document(database)
        result["revision"] = int(document.get("revision", 0) or 0)
        result["policies"] = document.get("policies", [])
        manifest = document.get("manifest")
        if isinstance(manifest, dict):
            result["manifest"] = {
                "catalog": manifest.get("catalog", result["manifest"]["catalog"]),
                "schema": manifest.get("schema", result["manifest"]["schema"]),
                "models": manifest.get("models", []),
                "relationships": manifest.get(
                    "relationships", manifest.get("relations", [])
                ),
            }
        return result
    return MdlManifest.from_dict(document).to_document()


def _load_database_document(manifest_path: Path, database: str) -> dict[str, Any]:
    if manifest_path.exists():
        active = _read_json(manifest_path)
        if _document_database(active) == database:
            return _document_to_api_response(active)
    snapshot_path = _database_snapshot_path(manifest_path, database)
    if snapshot_path.exists():
        snapshot = _read_json(snapshot_path)
        if _document_database(snapshot) != database:
            raise MdlConfigError(
                "MDL database snapshot mismatch: "
                f"expected {database}, got {_document_database(snapshot)}."
            )
        return _document_to_api_response(snapshot)
    return _empty_database_document(database)


def _save_active_database_snapshot(manifest_path: Path) -> None:
    active = _read_json(manifest_path)
    active_database = _document_database(active)
    _atomic_write_json(_database_snapshot_path(manifest_path, active_database), active)


def _current_database_revision(manifest_path: Path, database: str) -> int:
    if manifest_path.exists():
        active = _read_json(manifest_path)
        if _document_database(active) == database:
            return int(active.get("revision", 0) or 0)
    snapshot_path = _database_snapshot_path(manifest_path, database)
    if snapshot_path.exists():
        snapshot = _read_json(snapshot_path)
        if _document_database(snapshot) != database:
            raise MdlConfigError(
                "MDL database snapshot mismatch: "
                f"expected {database}, got {_document_database(snapshot)}."
            )
        return int(snapshot.get("revision", 0) or 0)
    return 0
