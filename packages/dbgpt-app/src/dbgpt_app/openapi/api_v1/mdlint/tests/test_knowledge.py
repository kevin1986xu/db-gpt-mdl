from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dbgpt_app.openapi.api_v1.mdlint.knowledge import (
    KnowledgeService,
    manifest_fingerprint,
    resolve_knowledge_path,
)
from dbgpt_app.openapi.api_v1.mdlint.service import MdlQueryService


class FakePlanner:
    def dry_plan(
        self,
        sql: str,
        manifest: Any,
        data_source: str,
        connection_info: dict[str, Any] | None = None,
    ) -> str:
        return f"SELECT * FROM ({sql.strip().rstrip(';')}) AS planned"


class FakeQueryService(MdlQueryService):
    planned: list[tuple[str, str | None, str | None, str]] = []

    def __init__(self, manifest_path: str | Path | None = None):
        super().__init__(manifest_path=manifest_path, planner=FakePlanner())

    def plan_authorized_sql(self, *args, **kwargs):
        FakeQueryService.planned.append(
            (
                args[0],
                kwargs.get("user_id"),
                kwargs.get("role"),
                kwargs.get("data_source"),
            )
        )
        return super().plan_authorized_sql(*args, **kwargs)


class FakeConnector:
    db_type = "duckdb"

    def __init__(self) -> None:
        self.queries: list[str] = []

    def query_ex(self, query: str, *args, **kwargs):
        self.queries.append(query)
        return ["ok"], [(1,)]


def _document(database: str = "drone_manage") -> dict[str, Any]:
    return {
        "database": database,
        "revision": 0,
        "manifest": {
            "schema": "public",
            "models": [
                {
                    "name": "device_registration",
                    "tableReference": {
                        "schema": "public",
                        "table": "device_registration",
                    },
                    "columns": [
                        {"name": "id", "type": "integer"},
                        {"name": "device_code", "type": "varchar"},
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
                    "device_registration": {
                        "columns": ["id", "device_code"],
                        "filters": [],
                    }
                },
            }
        ],
    }


def _qa(entry_id: str = "11111111-1111-1111-1111-111111111111") -> dict[str, Any]:
    return {
        "id": entry_id,
        "kind": "qa_sql",
        "title": "无人机数量",
        "question": "查询无人机数量",
        "content": "统计已登记无人机设备编码数量。",
        "sql": 'SELECT COUNT(device_code) AS count FROM "device_registration"',
        "aliases": ["飞机数量", "设备数量"],
        "models": {"device_registration": ["device_code"]},
        "status": "draft",
    }


def _business() -> dict[str, Any]:
    return {
        "id": "22222222-2222-2222-2222-222222222222",
        "kind": "business_rule",
        "title": "登记状态口径",
        "question": "",
        "content": "登记状态用于区分设备当前流程状态。",
        "sql": "",
        "aliases": ["设备状态"],
        "models": {"device_registration": ["status"]},
        "status": "draft",
    }


def _services(tmp_path: Path) -> tuple[MdlQueryService, KnowledgeService, Path]:
    manifest_path = tmp_path / "mdl.json"
    mdl = MdlQueryService(manifest_path=manifest_path)
    mdl.save(_document(), revision=0)
    knowledge = KnowledgeService(
        manifest_path=manifest_path,
        query_service_factory=FakeQueryService,
    )
    return mdl, knowledge, manifest_path


def test_draft_entries_are_not_returned_at_runtime(tmp_path: Path) -> None:
    _, knowledge, _ = _services(tmp_path)
    saved = knowledge.save("drone_manage", [_qa()], revision=0)

    matches = knowledge.search(
        "drone_manage",
        "无人机数量",
        user_id="admin",
        role="admin",
        data_source="duckdb",
    )

    assert saved["entries"][0]["status"] == "draft"
    assert matches == []


@pytest.mark.parametrize("question", ["人员数量是多少", "今天下雨吗", "土地面积是多少"])
def test_device_classification_does_not_match_unrelated_chinese_questions(
    tmp_path: Path, question: str
) -> None:
    _, knowledge, _ = _services(tmp_path)
    rule = _business()
    rule.update(
        title="设备分类口径",
        content="0 无人机 2 遥控器 3 机场",
        aliases=["无人机", "遥控器", "机场", "设备分类"],
    )
    saved = knowledge.save("drone_manage", [rule], revision=0)
    knowledge.approve("drone_manage", rule["id"], saved["revision"])
    assert knowledge.search("drone_manage", question, "admin", "admin") == []


def test_approve_validates_and_runtime_search_matches_alias_and_chinese(
    tmp_path: Path,
) -> None:
    _, knowledge, _ = _services(tmp_path)
    saved = knowledge.save("drone_manage", [_qa()], revision=0)
    connector = FakeConnector()

    approved = knowledge.approve(
        "drone_manage",
        saved["entries"][0]["id"],
        saved["revision"],
        connector=connector,
        data_source="duckdb",
    )
    matches = knowledge.search(
        "drone_manage",
        "飞机数量是多少",
        user_id="admin",
        role="admin",
        data_source="duckdb",
    )

    assert approved["entries"][0]["status"] == "approved"
    assert approved["entries"][0]["mdl_fingerprint"] == manifest_fingerprint(
        _document()
    )
    assert connector.queries[0].startswith("EXPLAIN SELECT * FROM")
    assert matches[0]["id"] == saved["entries"][0]["id"]
    assert matches[0]["kind"] == "qa_sql"
    assert matches[0]["stale"] is False
    assert set(matches[0]) == {
        "status",
        "stale",
        "id",
        "title",
        "kind",
        "question",
        "content",
        "sql",
        "models",
    }


def test_runtime_search_ignores_low_signal_quantity_overlap(tmp_path: Path) -> None:
    _, knowledge, _ = _services(tmp_path)
    qa = _qa()
    qa["question"] = "无人机数量是多少"
    saved = knowledge.save("drone_manage", [qa], revision=0)
    knowledge.approve(
        "drone_manage",
        saved["entries"][0]["id"],
        saved["revision"],
        connector=FakeConnector(),
        data_source="duckdb",
    )

    assert (
        knowledge.search(
            "drone_manage",
            "人员数量是多少",
            user_id="admin",
            role="admin",
            data_source="duckdb",
        )
        == []
    )
    assert knowledge.search(
        "drone_manage",
        "无人机数量是多少",
        user_id="admin",
        role="admin",
        data_source="duckdb",
    )


def test_runtime_search_preserves_long_sql_without_truncation(tmp_path: Path) -> None:
    _, knowledge, _ = _services(tmp_path)
    entry = _qa()
    long_sql = (
        'SELECT device_code FROM "device_registration" WHERE '
        + " OR ".join(f"device_code = 'D{index:04d}'" for index in range(500))
    )
    entry["sql"] = long_sql
    entry["content"] = "完整 SQL 不能被知识库检索层截断。"
    saved = knowledge.save("drone_manage", [entry], revision=0)
    approved = knowledge.approve(
        "drone_manage",
        saved["entries"][0]["id"],
        saved["revision"],
        connector=FakeConnector(),
        data_source="duckdb",
    )

    matches = knowledge.search(
        "drone_manage",
        "完整 SQL",
        user_id="admin",
        role="admin",
        data_source="duckdb",
    )

    assert approved["entries"][0]["sql"] == long_sql
    assert matches[0]["sql"] == long_sql


def test_forged_approved_and_edited_approved_entries_are_demoted(
    tmp_path: Path,
) -> None:
    _, knowledge, _ = _services(tmp_path)
    forged = _qa()
    forged["status"] = "approved"
    forged["mdl_fingerprint"] = "forged"

    saved = knowledge.save("drone_manage", [forged], revision=0)
    approved = knowledge.approve(
        "drone_manage",
        saved["entries"][0]["id"],
        saved["revision"],
        connector=FakeConnector(),
        data_source="duckdb",
    )
    edited = approved["entries"][0] | {"content": "新的审核口径"}
    demoted = knowledge.save("drone_manage", [edited], revision=approved["revision"])

    assert saved["entries"][0]["status"] == "draft"
    assert demoted["entries"][0]["status"] == "draft"
    assert "mdl_fingerprint" not in demoted["entries"][0]


def test_approved_entries_become_stale_when_manifest_changes(tmp_path: Path) -> None:
    mdl, knowledge, _ = _services(tmp_path)
    saved = knowledge.save("drone_manage", [_qa()], revision=0)
    approved = knowledge.approve(
        "drone_manage",
        saved["entries"][0]["id"],
        saved["revision"],
        connector=FakeConnector(),
        data_source="duckdb",
    )
    document = mdl.load("drone_manage")
    document["manifest"]["models"][0]["columns"].append(
        {"name": "serial_no", "type": "varchar"}
    )
    mdl.save(document, revision=1)

    loaded = knowledge.load("drone_manage", include_stale=True)
    matches = knowledge.search(
        "drone_manage",
        "无人机数量",
        user_id="admin",
        role="admin",
        data_source="duckdb",
    )

    assert approved["entries"][0]["status"] == "approved"
    assert loaded["entries"][0]["stale"] is True
    assert matches == []


def test_cross_database_storage_is_isolated(tmp_path: Path) -> None:
    mdl, knowledge, _ = _services(tmp_path)
    mdl.save(_document("other_db"), revision=0)

    knowledge.save("drone_manage", [_qa()], revision=0)

    assert knowledge.load("other_db")["entries"] == []


def test_hidden_bound_field_prevents_runtime_leakage(tmp_path: Path) -> None:
    _, knowledge, _ = _services(tmp_path)
    saved = knowledge.save("drone_manage", [_business()], revision=0)
    approved = knowledge.approve(
        "drone_manage",
        saved["entries"][0]["id"],
        saved["revision"],
        connector=FakeConnector(),
        data_source="duckdb",
    )

    matches = knowledge.search(
        "drone_manage",
        "设备状态",
        user_id="analyst",
        role="analyst",
        data_source="duckdb",
    )

    assert approved["entries"][0]["status"] == "approved"
    assert matches == []


def test_write_sql_is_rejected_on_approval(tmp_path: Path) -> None:
    _, knowledge, _ = _services(tmp_path)
    entry = _qa()
    entry["sql"] = 'DELETE FROM "device_registration"'
    saved = knowledge.save("drone_manage", [entry], revision=0)

    with pytest.raises(Exception, match="read-only"):
        knowledge.approve(
            "drone_manage",
            saved["entries"][0]["id"],
            saved["revision"],
            connector=FakeConnector(),
            data_source="duckdb",
        )


def test_revision_conflict_does_not_overwrite(tmp_path: Path) -> None:
    _, knowledge, _ = _services(tmp_path)
    knowledge.save("drone_manage", [_qa()], revision=0)

    with pytest.raises(Exception, match="revision conflict"):
        knowledge.save("drone_manage", [_business()], revision=0)

    assert knowledge.load("drone_manage")["entries"][0]["kind"] == "qa_sql"


def test_path_traversal_database_name_uses_hash(tmp_path: Path) -> None:
    manifest_path = tmp_path / "mdl.json"
    database = "../outside/../../tenant?name=beta"

    path = resolve_knowledge_path(manifest_path, database)

    assert path.parent == tmp_path / "mdl.knowledge"
    assert path.name.endswith(".json")
    assert "/" not in path.name
    assert ".." not in path.name
