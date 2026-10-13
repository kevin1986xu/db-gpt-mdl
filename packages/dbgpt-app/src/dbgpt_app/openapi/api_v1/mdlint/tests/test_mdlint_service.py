from __future__ import annotations

import json
import sys
import types
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import pytest

from dbgpt_app.openapi.api_v1.mdlint import (
    MdlConfigError,
    MdlPolicyError,
    MdlQueryService,
    wrap_connector,
)
from dbgpt_app.openapi.api_v1.mdlint.models import normalize_manifest


class FakePlanner:
    def dry_plan(
        self,
        sql: str,
        manifest: Any,
        data_source: str,
        connection_info: dict[str, Any] | None = None,
    ) -> str:
        return f"WITH wren AS ({sql}) SELECT * FROM wren"


class FakeConnector:
    dialect = "duckdb"

    def __init__(self) -> None:
        self.sql: str | None = None

    def query_ex(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        fetch: str = "all",
        timeout: float | None = None,
    ):
        self.sql = query
        return ["id", "customer_name"], [(1, "Alice")]


def _write_manifest(path: Path) -> None:
    path.write_text(json.dumps(_document()), encoding="utf-8")


def _document() -> dict[str, Any]:
    return {
        "database": "mdl_demo",
        "revision": 1,
        "manifest": {
            "models": [
                {
                    "name": "orders",
                    "tableReference": {"schema": "main", "table": "raw_orders"},
                    "primaryKey": "id",
                    "columns": [
                        {"name": "id", "type": "integer"},
                        {"name": "customer_id", "type": "integer"},
                        {"name": "amount", "type": "double"},
                        {"name": "region", "type": "varchar"},
                        {
                            "name": "customer",
                            "type": "customers",
                            "relationship": "orders_customers",
                        },
                        {
                            "name": "customer_name",
                            "type": "varchar",
                            "isCalculated": True,
                            "expression": "customer.name",
                        },
                    ],
                },
                {
                    "name": "customers",
                    "tableReference": {"schema": "main", "table": "raw_customers"},
                    "primaryKey": "id",
                    "columns": [
                        {"name": "id", "type": "integer"},
                        {"name": "name", "type": "varchar"},
                        {"name": "region", "type": "varchar"},
                        {"name": "email", "type": "varchar"},
                    ],
                },
            ],
            "relationships": [
                {
                    "name": "orders_customers",
                    "models": ["orders", "customers"],
                    "joinType": "MANY_TO_ONE",
                    "condition": "orders.customer_id = customers.id",
                }
            ],
        },
        "policies": [
            {
                "username": "analyst",
                "models": {
                    "orders": {
                        "columns": [
                            "id",
                            "customer_id",
                            "amount",
                            "region",
                            "customer_name",
                        ],
                        "filters": [{"column": "region", "op": "eq", "value": "east"}],
                    },
                    "customers": {
                        "columns": ["id", "name", "region"],
                        "filters": [{"column": "region", "op": "eq", "value": "east"}],
                    },
                },
            }
        ],
    }


def _device_registration_document() -> dict[str, Any]:
    return {
        "database": "drone_manage",
        "revision": 1,
        "manifest": {
            "models": [
                {
                    "name": "device_registration",
                    "tableReference": {
                        "schema": "public",
                        "table": "device_registration",
                    },
                    "primaryKey": "id",
                    "columns": [
                        {
                            "name": "id",
                            "type": "integer",
                            "display_name": "登记ID",
                            "description": "设备登记主键",
                            "properties": {
                                "displayName": "登记ID属性",
                                "description": "设备登记主键属性描述",
                            },
                        },
                        {
                            "name": "device_code",
                            "type": "varchar",
                            "display_name": "设备编码",
                            "description": "无人机在登记系统中的唯一业务编码",
                            "properties": {
                                "displayName": "设备编码属性",
                                "description": "登记系统展示的设备编码业务说明",
                                "owner": "registration",
                            },
                        },
                        {
                            "name": "registered_at",
                            "type": "timestamp",
                            "display_name": "登记时间",
                            "description": "设备完成注册登记的时间",
                            "properties": {
                                "displayName": "登记时间属性",
                                "description": "登记时间属性描述",
                                "source": "device_registration",
                            },
                        },
                    ],
                }
            ],
            "relationships": [],
        },
        "policies": [],
    }


def _column(
    document: dict[str, Any],
    model_name: str,
    column_name: str,
) -> dict[str, Any]:
    model = next(
        model
        for model in document["manifest"]["models"]
        if model["name"] == model_name
    )
    return next(
        column for column in model["columns"] if column["name"] == column_name
    )


def test_effective_manifest_prunes_columns_and_injects_filters(tmp_path: Path) -> None:
    manifest_path = tmp_path / "mdl.json"
    _write_manifest(manifest_path)
    service = MdlQueryService(manifest_path=manifest_path, planner=FakePlanner())

    effective = service.effective_manifest("analyst")
    orders = next(model for model in effective["models"] if model["name"] == "orders")
    customers = next(
        model for model in effective["models"] if model["name"] == "customers"
    )

    assert "tableReference" not in orders
    assert """WHERE "region" = 'east'""" in orders["refSql"]
    assert """WHERE "region" = 'east'""" in customers["refSql"]
    assert {column["name"] for column in orders["columns"]} == {
        "id",
        "customer_id",
        "amount",
        "region",
        "customer",
        "customer_name",
    }
    assert {column["name"] for column in customers["columns"]} == {
        "id",
        "name",
        "region",
    }


def test_unknown_user_gets_empty_effective_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "mdl.json"
    _write_manifest(manifest_path)
    service = MdlQueryService(manifest_path=manifest_path, planner=FakePlanner())

    assert service.effective_manifest("missing")["models"] == []
    assert service.get_schema("missing") == {}


def test_missing_policy_cannot_plan_query(tmp_path: Path) -> None:
    manifest_path = tmp_path / "mdl.json"
    _write_manifest(manifest_path)
    service = MdlQueryService(manifest_path=manifest_path)

    with pytest.raises(Exception):
        service.plan_authorized_sql(
            "SELECT id FROM orders",
            user_id="missing",
            role="analyst",
            data_source="duckdb",
        )


def test_cte_and_subquery_cannot_read_denied_columns(tmp_path: Path) -> None:
    manifest_path = tmp_path / "mdl.json"
    _write_manifest(manifest_path)
    service = MdlQueryService(manifest_path=manifest_path)
    service.save_policies(
        [
            {
                "username": "analyst",
                "models": {
                    "orders": {
                        "columns": ["id", "customer_id", "region"],
                        "filters": [],
                    }
                },
            }
        ],
        revision=1,
    )

    for sql in (
        "WITH x AS (SELECT cost FROM orders) SELECT * FROM x",
        "SELECT id FROM (SELECT id, cost FROM orders) x",
    ):
        assert "cost" not in {
            column["name"]
            for column in service.get_schema("analyst").get("orders", [])
        }
        with pytest.raises(Exception):
            service.plan_authorized_sql(
                sql,
                user_id="analyst",
                role="analyst",
                data_source="duckdb",
            )


def test_admin_role_bypasses_policy(tmp_path: Path) -> None:
    manifest_path = tmp_path / "mdl.json"
    _write_manifest(manifest_path)
    service = MdlQueryService(manifest_path=manifest_path, planner=FakePlanner())

    schema = service.get_schema("admin", role="admin")

    assert "email" in {column["name"] for column in schema["customers"]}


def test_calculated_field_is_removed_when_dependency_is_hidden(tmp_path: Path) -> None:
    manifest_path = tmp_path / "mdl.json"
    _write_manifest(manifest_path)
    service = MdlQueryService(manifest_path=manifest_path, planner=FakePlanner())
    service.save_policies(
        [
            {
                "username": "analyst",
                "models": {
                    "orders": {
                        "columns": [
                            "id",
                            "customer_id",
                            "region",
                            "customer_name",
                        ],
                        "filters": [],
                    },
                    "customers": {
                        "columns": ["id", "region"],
                        "filters": [],
                    },
                },
            }
        ],
        revision=1,
    )

    orders = service.get_schema("analyst")["orders"]

    assert "customer_name" not in {column["name"] for column in orders}


def test_service_loads_and_updates_document_shape(tmp_path: Path) -> None:
    manifest_path = tmp_path / "mdl.json"
    _write_manifest(manifest_path)
    service = MdlQueryService(manifest_path=manifest_path, planner=FakePlanner())

    saved = service.save(service.load(), revision=1)
    assert saved.revision == 2

    updated = service.save_policies(
        [
            {
                "username": "analyst",
                "models": {
                    "orders": {"columns": ["id", "customer_id"], "filters": []}
                },
            }
        ],
        revision=2,
    )

    assert updated.revision == 3
    assert service.get_schema("analyst") == {
        "orders": [
            {"name": "id", "type": "integer"},
            {"name": "customer_id", "type": "integer"},
        ]
    }


def test_service_preserves_device_registration_annotations_after_save_load(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "mdl.json"
    manifest_path.write_text(
        json.dumps(_device_registration_document()),
        encoding="utf-8",
    )
    service = MdlQueryService(manifest_path=manifest_path, planner=FakePlanner())
    original_other_field = json.loads(
        json.dumps(_column(service.load(), "device_registration", "registered_at"))
    )

    first_saved = service.save(service.load(), revision=1)
    first_loaded = service.load()
    second_saved = service.save(first_loaded, revision=first_saved.revision)
    second_loaded = service.load()
    annotated = _column(second_loaded, "device_registration", "device_code")

    assert first_saved.revision == 2
    assert second_saved.revision == 3
    assert annotated["display_name"] == "设备编码"
    assert annotated["description"] == "无人机在登记系统中的唯一业务编码"
    assert annotated["properties"]["displayName"] == "设备编码属性"
    assert annotated["properties"]["description"] == "登记系统展示的设备编码业务说明"
    assert annotated["properties"]["owner"] == "registration"
    assert _column(second_loaded, "device_registration", "registered_at") == (
        original_other_field
    )


def test_service_revision_conflict_does_not_overwrite_device_registration_annotations(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "mdl.json"
    manifest_path.write_text(
        json.dumps(_device_registration_document()),
        encoding="utf-8",
    )
    service = MdlQueryService(manifest_path=manifest_path, planner=FakePlanner())

    baseline = service.load()
    stale_document = json.loads(json.dumps(baseline))
    stale_column = _column(stale_document, "device_registration", "device_code")
    stale_column["display_name"] = "过期保存的设备编码"
    stale_column["description"] = "这次过期保存不能覆盖磁盘文件"
    stale_column["properties"]["displayName"] = "过期属性显示名"
    stale_column["properties"]["description"] = "过期属性描述"

    winning_document = service.load()
    winning_column = _column(winning_document, "device_registration", "device_code")
    winning_column["display_name"] = "当前设备编码"
    winning_column["description"] = "当前版本中的设备编码业务描述"
    winning_column["properties"]["displayName"] = "当前属性显示名"
    winning_column["properties"]["description"] = "当前属性描述"
    saved = service.save(winning_document, revision=1)

    with pytest.raises(MdlConfigError, match="revision conflict"):
        service.save(stale_document, revision=1)

    assert saved.revision == 2
    loaded = service.load()
    annotated = _column(loaded, "device_registration", "device_code")
    assert annotated["display_name"] == "当前设备编码"
    assert annotated["description"] == "当前版本中的设备编码业务描述"
    assert annotated["properties"]["displayName"] == "当前属性显示名"
    assert annotated["properties"]["description"] == "当前属性描述"
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == loaded


def test_wrap_connector_uses_current_principal_and_raw_query_ex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "mdl.json"
    _write_manifest(manifest_path)
    monkeypatch.setenv("DBGPT_MDL_PATH", str(manifest_path))
    monkeypatch.setenv("DBGPT_MDL_ENABLED", "1")

    current_principal: ContextVar[dict[str, str] | None] = ContextVar(
        "current_principal",
        default=None,
    )
    auth_module = types.ModuleType("dbgpt_app.openapi.api_v1.mdlint.auth")
    auth_module.current_principal = current_principal
    monkeypatch.setitem(
        sys.modules,
        "dbgpt_app.openapi.api_v1.mdlint.auth",
        auth_module,
    )

    raw = FakeConnector()
    with pytest.raises(MdlPolicyError, match="authenticated"):
        wrap_connector(raw, "mdl_demo")

    token = current_principal.set({"username": "analyst", "role": "analyst"})
    try:
        protected = wrap_connector(raw, "mdl_demo")
        protected._service = MdlQueryService(
            manifest_path=manifest_path,
            planner=FakePlanner(),
        )
        columns, rows = protected.query_ex("SELECT id, customer_name FROM orders")
        table_info = protected.get_table_info()
        simple_info = protected.table_simple_info()
    finally:
        current_principal.reset(token)

    assert protected.mdl_enabled is True
    assert columns == ["id", "customer_name"]
    assert rows == [(1, "Alice")]
    assert raw.sql == (
        "SELECT * FROM (WITH wren AS (SELECT id, customer_name FROM orders) "
        "SELECT * FROM wren) AS mdl_result LIMIT 1001"
    )
    assert "CREATE TABLE orders" in table_info
    assert "orders(id,customer_id,amount,region,customer_name);" in simple_info


def test_connector_reloads_policy_and_isolates_captured_principal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "mdl.json"
    _write_manifest(manifest_path)
    monkeypatch.setenv("DBGPT_MDL_PATH", str(manifest_path))
    monkeypatch.setenv("DBGPT_MDL_ENABLED", "1")

    current_principal: ContextVar[dict[str, str] | None] = ContextVar(
        "current_principal",
        default=None,
    )
    auth_module = types.ModuleType("dbgpt_app.openapi.api_v1.mdlint.auth")
    auth_module.current_principal = current_principal
    monkeypatch.setitem(
        sys.modules,
        "dbgpt_app.openapi.api_v1.mdlint.auth",
        auth_module,
    )

    raw = FakeConnector()
    token = current_principal.set({"username": "analyst", "role": "analyst"})
    try:
        protected = wrap_connector(raw, "mdl_demo")
        assert protected.get_table_names() == ["orders", "customers"]
        service = MdlQueryService(manifest_path=manifest_path, planner=FakePlanner())
        service.save_policies(
            [
                {
                    "username": "analyst",
                    "models": {
                        "orders": {"columns": ["id", "customer_id"], "filters": []}
                    },
                }
            ],
            revision=1,
        )
        assert protected.get_table_names() == ["orders"]
    finally:
        current_principal.reset(token)

    token = current_principal.set({"username": "admin", "role": "admin"})
    try:
        with pytest.raises(MdlPolicyError, match="principal changed"):
            protected.get_table_names()
    finally:
        current_principal.reset(token)


def test_wrap_connector_returns_original_for_non_target_db() -> None:
    connector = FakeConnector()

    assert wrap_connector(connector, "other_db") is connector


def test_wrap_connector_requires_enable_env_even_when_config_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "mdl.json"
    _write_manifest(manifest_path)
    monkeypatch.setenv("DBGPT_MDL_PATH", str(manifest_path))
    monkeypatch.delenv("DBGPT_MDL_ENABLED", raising=False)

    connector = FakeConnector()

    assert wrap_connector(connector, "mdl_demo") is connector


def test_normalize_manifest_uses_manifest_schema_for_legacy_table_models() -> None:
    normalized = normalize_manifest(
        {
            "schema": "public",
            "models": [
                {
                    "name": "device_registration",
                    "table": "device_registration",
                    "columns": [],
                }
            ],
        }
    )

    assert normalized["models"][0]["tableReference"] == {
        "schema": "public",
        "table": "device_registration",
    }
