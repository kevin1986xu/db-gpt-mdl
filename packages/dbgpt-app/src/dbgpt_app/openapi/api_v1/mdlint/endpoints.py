"""MDL management inside the existing DB-GPT API and connector chain."""

import time
from copy import deepcopy
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from .auth import (
    COOKIE,
    authenticate,
    current_principal,
    enabled,
    initialize_credentials,
)
from .models import normalize_name
from .service import get_service
from .wren_adapter import WrenPlanner

router = APIRouter(prefix="/v1/mdl")


class LoginRequest(BaseModel):
    token: str


class SaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest: dict[str, Any]
    revision: int | None = None
    database: str | None = None


class PolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policies: list[dict[str, Any]]
    revision: int
    database: str | None = None


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(min_length=1, max_length=50000)
    limit: int = Field(default=100, ge=1, le=1000)
    database: str | None = None


def result(data):
    return {"success": True, "err_code": None, "err_msg": None, "data": data}


def require_user(admin=False):
    if not enabled():
        raise HTTPException(503, "MDL integration is disabled")
    principal = current_principal.get()
    if not principal:
        raise HTTPException(401, "请先登录 MDL 账号")
    if admin and principal["role"] != "admin":
        raise HTTPException(403, "该操作需要管理员权限")
    return principal


def manager():
    from dbgpt._private.config import Config

    return Config().local_db_manager


def fail(exc):
    status = 409 if "conflict" in str(exc).lower() else 400
    raise HTTPException(status, str(exc)) from exc


def validate_on_database(document, database: str | None = None):
    """Validate actual Wren plans and physical columns before publishing."""
    service = get_service()
    selected_database = (
        database or document.get("database") or service.load()["database"]
    )
    raw = manager()._get_raw_connector(selected_database)
    schema_name = getattr(raw, "_schema", None)
    if not schema_name:
        schema_name = (
            "main"
            if getattr(raw, "db_type", "") in {"duckdb", "sqlite"}
            else document.get("manifest", {}).get("schema", "public")
        )
    # Older UI payloads only sent `table`; attach the selected connector schema
    # before Wren normalizes the model to a tableReference.
    for model in document.get("manifest", {}).get("models", []):
        if (
            isinstance(model, dict)
            and model.get("table")
            and not model.get("tableReference")
        ):
            model["tableReference"] = {"schema": schema_name, "table": model["table"]}
    parsed = service.validate(document)
    physical = set(raw.get_table_names())
    for model in parsed.raw.get("models", []):
        reference = model.get("tableReference", {})
        if (
            not reference
            or reference.get("table") not in physical
            or reference.get("catalog")
            or model.get("refSql")
        ):
            raise ValueError(
                "tableReference 必须引用当前数据源中已存在的表，"
                "且不能使用 catalog 或 refSql"
            )
        name = model["name"].replace('"', '""')
        sql = WrenPlanner().dry_plan(
            f'SELECT * FROM "{name}" LIMIT 0',
            parsed,
            service.map_data_source(raw),
        )
        raw.query_ex("EXPLAIN " + sql)
    # Policy manifests need independent validation, including derived fields.
    for policy in parsed.raw_policies:
        effective = parsed.effective_manifest(policy["username"], role="analyst")
        if not effective.get("models"):
            continue
        restricted = service.validate({"manifest": effective})
        for model in effective.get("models", []):
            name = model["name"].replace('"', '""')
            sql = WrenPlanner().dry_plan(
                f'SELECT * FROM "{name}" LIMIT 0',
                restricted,
                service.map_data_source(raw),
            )
            raw.query_ex("EXPLAIN " + sql)
    return parsed


def _prune_policies_for_manifest(document: dict[str, Any]) -> None:
    """Drop grants for models/fields removed by a manifest replacement.

    The UI saves the manifest before saving policy edits. Keeping grants from
    the previous manifest would make an otherwise valid table change fail with
    an "unknown MDL model" error, so stale grants are narrowed before parsing.
    """
    service = get_service()
    parsed = service.validate(
        {"manifest": document.get("manifest", {}), "policies": []}
    )
    model_columns = {
        normalize_name(model_name): set(model.columns)
        for model_name, model in parsed.models.items()
    }
    policies = []
    for raw_policy in document.get("policies", []):
        if not isinstance(raw_policy, dict):
            policies.append(raw_policy)
            continue
        policy = deepcopy(raw_policy)
        raw_models = policy.get("models")
        if not isinstance(raw_models, dict):
            policies.append(policy)
            continue
        kept_models: dict[str, Any] = {}
        for raw_name, raw_grant in raw_models.items():
            columns = model_columns.get(normalize_name(str(raw_name)))
            if columns is None or not isinstance(raw_grant, dict):
                continue
            grant = deepcopy(raw_grant)
            raw_columns = grant.get("columns")
            if isinstance(raw_columns, list):
                grant["columns"] = [
                    column
                    for column in raw_columns
                    if isinstance(column, str) and normalize_name(column) in columns
                ]
                if not grant["columns"]:
                    continue
            raw_filters = grant.get("filters")
            if isinstance(raw_filters, list):
                grant["filters"] = [
                    rule
                    for rule in raw_filters
                    if isinstance(rule, dict)
                    and isinstance(rule.get("column"), str)
                    and normalize_name(rule["column"]) in columns
                ]
            kept_models[raw_name] = grant
        policy["models"] = kept_models
        policies.append(policy)
    document["policies"] = policies


@router.get("/session")
def session():
    if enabled():
        initialize_credentials()
    return result({"user": current_principal.get(), "enabled": enabled()})


@router.post("/login")
def login(payload: LoginRequest, response: Response):
    if not enabled():
        raise HTTPException(503, "MDL integration is disabled")
    initialize_credentials()
    principal = authenticate(payload.token)
    if not principal:
        raise HTTPException(401, "MDL token 无效")
    response.set_cookie(
        COOKIE, payload.token, httponly=True, samesite="strict", max_age=28800
    )
    return result({"user": principal, "enabled": True})


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return result({"user": None})


@router.get("")
def get_document(database: str | None = Query(default=None)):
    require_user(admin=True)
    return result({**get_service().load(database), "enabled": enabled()})


@router.put("")
def save_document(payload: SaveRequest):
    require_user(admin=True)
    if payload.revision is None:
        raise HTTPException(400, "保存必须携带 revision")
    try:
        document = get_service().load(payload.database)
        document["manifest"] = payload.manifest
        if payload.database:
            document["database"] = payload.database
        _prune_policies_for_manifest(document)
        validate_on_database(document, database=payload.database)
        saved = get_service().save(document, revision=payload.revision)
        # Keep request-scoped connector wrapping aligned with the datasource
        # selected in the MDL workbench without requiring a process restart.
        import os

        os.environ["DBGPT_MDL_DATABASE"] = saved.database or "mdl_demo"
        return result({**saved.to_document(), "enabled": True})
    except Exception as exc:
        fail(exc)


@router.post("/validate")
def validate_document(payload: SaveRequest):
    require_user(admin=True)
    try:
        document = get_service().load(payload.database)
        document["manifest"] = payload.manifest
        if payload.database:
            document["database"] = payload.database
        validate_on_database(document, database=payload.database)
        return result({"valid": True, "issues": []})
    except Exception as exc:
        return result(
            {"valid": False, "issues": [{"level": "error", "message": str(exc)}]}
        )


@router.get("/schema")
def schema(database: str | None = Query(default=None)):
    user = require_user()
    try:
        service = get_service()
        selected_database = database or service.load()["database"]
        if user["role"] == "admin":
            document = service.load(selected_database)
            manifest = document.get("manifest", {})
            data = {
                "models": manifest.get("models", []),
                "relationships": manifest.get(
                    "relationships", manifest.get("relations", [])
                ),
            }
        else:
            effective = service.effective_manifest(user["username"], role=user["role"])
            data = {
                "models": effective.get("models", []),
                "relationships": effective.get("relationships", []),
            }
        if user["role"] == "admin":
            raw = manager()._get_raw_connector(selected_database)
            schema_name = getattr(raw, "_schema", None)
            if not schema_name:
                schema_name = (
                    "main"
                    if getattr(raw, "db_type", "") in {"duckdb", "sqlite"}
                    else "public"
                )
            data["tables"] = [
                {
                    "name": table,
                    "schema": schema_name,
                    "columns": [
                        {"name": c["name"], "type": str(c.get("type", ""))}
                        for c in raw.get_columns(table)
                    ],
                }
                for table in sorted(raw.get_table_names())
            ]
            data["database"] = selected_database
        else:
            # Do not disclose physical table names, predicates, or backing SQL.
            data["models"] = [
                {
                    "name": m["name"],
                    "columns": [
                        {"name": c["name"], "type": c.get("type", "")}
                        for c in m["columns"]
                        if not c.get("relationship")
                    ],
                }
                for m in data["models"]
            ]
        return result(data)
    except Exception as exc:
        fail(exc)


@router.get("/policies")
def policies(database: str | None = Query(default=None)):
    require_user(admin=True)
    document = get_service().load(database)
    return result({"policies": document["policies"], "revision": document["revision"]})


@router.put("/policies")
def save_policies(payload: PolicyRequest):
    require_user(admin=True)
    service = get_service()
    if payload.database and service.load()["database"] != payload.database:
        raise HTTPException(
            409,
            "当前激活数据源已变化，请刷新后重新保存策略",
        )
    try:
        document = service.load(payload.database)
        document["policies"] = payload.policies
        validate_on_database(document, database=payload.database)
        saved = service.save_policies(
            payload.policies,
            revision=payload.revision,
            database=payload.database,
        )
        return result({"policies": saved.raw_policies, "revision": saved.revision})
    except Exception as exc:
        fail(exc)


@router.post("/query")
def query(payload: QueryRequest):
    require_user()
    active_database = get_service().load()["database"]
    if payload.database and payload.database != active_database:
        raise HTTPException(
            400,
            "请先保存所选数据源的MDL，使其生效后再验证SQL",
        )
    try:
        started = time.monotonic()
        connector = manager().get_connector(active_database)
        columns, rows = connector.query_ex(payload.sql)
        columns = [
            str(column[0]) if isinstance(column, (tuple, list)) else str(column)
            for column in columns
        ]
        rows = [
            row if isinstance(row, (tuple, list, dict)) else list(row)
            for row in (rows or [])
        ]
        return result(
            {
                "columns": columns,
                "rows": rows[: payload.limit],
                "logical_sql": payload.sql,
                "count": min(len(rows), payload.limit),
                "truncated": len(rows) > payload.limit,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        )
    except Exception as exc:
        fail(exc)
