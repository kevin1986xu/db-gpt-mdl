"""Native DB-GPT query/editor paths under the real Wren MDL proxy."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextvars import Token
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest

from dbgpt_app.openapi.api_v1.editor import api_editor_v1
from dbgpt_app.openapi.api_v1.mdlint.auth import current_principal
from dbgpt_app.openapi.api_v1.mdlint.connector import wrap_connector
from dbgpt_app.openapi.api_v1.mdlint.knowledge import KnowledgeService
from dbgpt_app.openapi.api_v1.tools.sql_query import make_sql_query


class RawConnector:
    db_type = "duckdb"
    dialect = "duckdb"

    def __init__(self, path: Path):
        self.path = path

    def query_ex(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        fetch: str = "all",
        timeout: float | None = None,
    ):
        with duckdb.connect(str(self.path), read_only=True) as db:
            cursor = db.execute(sql, params or {})
            return [column[0] for column in cursor.description], cursor.fetchall()

    def get_table_names(self):
        return ["raw_orders"]

    def get_columns(self, table: str):
        return [
            {"name": "id", "type": "INTEGER"},
            {"name": "amount", "type": "DOUBLE"},
            {"name": "status", "type": "VARCHAR"},
        ]


class Manager:
    def __init__(self, connector):
        self.connector = connector

    def get_connector(self, db_name: str):
        return wrap_connector(self.connector, db_name)


class NoDbEditorService:
    def get_storage_conv(self, conv_uid: str):
        raise AssertionError(f"conversation lookup should not run: {conv_uid}")


def _write_mdl(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "manifest": {
                    "catalog": "wren",
                    "schema": "public",
                    "models": [
                        {
                            "name": "orders",
                            "tableReference": {
                                "schema": "main",
                                "table": "raw_orders",
                            },
                            "columns": [
                                {"name": "id", "type": "integer"},
                                {"name": "amount", "type": "double"},
                                {"name": "status", "type": "varchar"},
                            ],
                        }
                    ],
                    "relationships": [],
                },
                "policies": [
                    {
                        "username": "analyst",
                        "models": {
                            "orders": {
                                "columns": ["id", "amount", "status"],
                                "filters": [],
                            }
                        },
                    }
                ],
                "revision": 1,
                "database": "mdl_demo",
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def native_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "demo.duckdb"
    with duckdb.connect(str(database)) as db:
        db.execute("CREATE TABLE raw_orders(id INTEGER, amount DOUBLE, status VARCHAR)")
        db.execute(
            "INSERT INTO raw_orders VALUES "
            "(1, 1200, 'paid'), (2, 900, 'paid'), (3, 500, 'cancelled')"
        )

    mdl_path = tmp_path / "mdl.json"
    _write_mdl(mdl_path)
    monkeypatch.setenv("DBGPT_MDL_ENABLED", "1")
    monkeypatch.setenv("DBGPT_MDL_PATH", str(mdl_path))
    monkeypatch.setenv("DBGPT_MDL_DATABASE", "mdl_demo")

    raw = RawConnector(database)
    principal_token: Token = current_principal.set(
        {"username": "analyst", "role": "analyst"}
    )
    try:
        yield raw, Manager(raw)
    finally:
        current_principal.reset(principal_token)


def test_native_sql_query_tool_uses_wren_proxy(native_context):
    raw, manager = native_context
    connector = manager.get_connector("mdl_demo")
    tool = make_sql_query({"conv_id": "native-sql"}, connector)

    output = json.loads(
        tool(sql="SELECT SUM(amount) AS revenue FROM orders WHERE status = 'paid'")
    )

    assert output["chunks"][0]["output_type"] == "markdown"
    assert "2100.0" in output["chunks"][0]["content"]
    assert raw.query_ex is not None


def test_native_connector_injects_annotations_and_keeps_sql_governed(native_context):
    raw, manager = native_context
    manifest_path = Path(os.environ["DBGPT_MDL_PATH"])
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    amount = document["manifest"]["models"][0]["columns"][1]
    amount["display_name"] = "订单金额"
    amount["description"] = "金额单位为元"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    knowledge = KnowledgeService(manifest_path=manifest_path)
    entry_id = str(uuid.uuid4())
    sample_sql = "SELECT SUM(amount) AS revenue FROM orders WHERE status = 'paid'"
    knowledge.save(
        "mdl_demo",
        [
            {
                "id": entry_id,
                "kind": "qa_sql",
                "title": "已支付订单收入",
                "question": "订单金额统计",
                "content": "只统计已支付订单，金额单位为元。",
                "sql": sample_sql,
                "models": {"orders": ["amount", "status"]},
            }
        ],
        revision=0,
    )
    assert knowledge.search("mdl_demo", "订单金额统计", "analyst", "analyst") == []
    knowledge.approve("mdl_demo", entry_id, revision=1, connector=raw)

    connector = manager.get_connector("mdl_demo")
    context = connector.get_semantic_context("订单金额统计")
    assert "订单金额" in context
    assert "金额单位为元" in context
    assert "已支付订单收入" in context
    assert sample_sql in context
    assert "raw_orders" not in context
    columns, rows = connector.query_ex(
        "SELECT SUM(amount) AS revenue FROM orders WHERE status = 'paid'"
    )
    assert columns == ["revenue"]
    assert rows == [(2100.0,)]


def test_native_editor_sql_run_uses_wren_proxy(
    native_context,
    monkeypatch: pytest.MonkeyPatch,
):
    raw, manager = native_context
    monkeypatch.setattr(
        api_editor_v1,
        "CFG",
        SimpleNamespace(local_db_manager=manager),
    )

    result = asyncio.run(
        api_editor_v1.editor_sql_run(
            {
                "db_name": "mdl_demo",
                "sql": (
                    "SELECT SUM(amount) AS revenue FROM orders WHERE status = 'paid'"
                ),
            },
            editor_service=NoDbEditorService(),
        )
    )

    assert result.success is True
    assert result.data.colunms == ["revenue"]
    assert result.data.values == [("2100.0",)]


def test_native_chart_run_uses_wren_proxy(
    native_context,
    monkeypatch: pytest.MonkeyPatch,
):
    _, manager = native_context
    monkeypatch.setattr(
        api_editor_v1,
        "CFG",
        SimpleNamespace(local_db_manager=manager),
    )

    result = asyncio.run(
        api_editor_v1.chart_run(
            {
                "db_name": "mdl_demo",
                "chart_type": "bar",
                "sql": (
                    "SELECT status, SUM(amount) AS revenue "
                    "FROM orders GROUP BY status ORDER BY status"
                ),
            },
            editor_service=NoDbEditorService(),
        )
    )

    assert result.success is True
    assert result.data.sql_data.colunms == ["status", "revenue"]
    assert result.data.sql_data.values == [
        ("cancelled", "500.0"),
        ("paid", "2100.0"),
    ]
    assert result.data.chart_type == "bar"
