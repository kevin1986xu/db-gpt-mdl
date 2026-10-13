from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dbgpt_app.openapi.api_v1.mdlint.analysis_rules import validate_analysis_rules
from dbgpt_app.openapi.api_v1.mdlint.errors import MdlConfigError, MdlPolicyError
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


def _glossary() -> dict[str, Any]:
    return {
        "id": "33333333-3333-3333-3333-333333333333",
        "kind": "glossary",
        "title": "设备术语",
        "content": "业务术语解释",
        "terms": [{"term": "无人机", "meaning": "飞行设备", "aliases": ["飞行器"]}],
        "models": {"device_registration": ["device_code"]},
    }


def _analysis_rule() -> dict[str, Any]:
    return {
        "id": "44444444-4444-4444-4444-444444444444",
        "kind": "analysis_rule",
        "title": "设备计数规则",
        "content": "指标分析约束",
        "rule": {
            "metric": "设备数",
            "conditions": ["去重设备编码"],
            "guidance": "排除停用设备",
            "checks": [
                {
                    "column": "设备数",
                    "operator": "gte",
                    "value": 0,
                    "severity": "error",
                    "message": "设备数不能为负",
                }
            ],
            "report_requirements": ["注明统计口径"],
        },
        "models": {"device_registration": ["device_code"]},
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
        "aliases",
        "terms",
        "rule",
        "expected_result",
        "reuse_on_exact_match",
        "validation",
        "exact_match",
        "conflict",
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
    long_sql = 'SELECT device_code FROM "device_registration" WHERE ' + " OR ".join(
        f"device_code = 'D{index:04d}'" for index in range(500)
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


def test_structured_knowledge_is_kind_specific_and_normalized(tmp_path: Path) -> None:
    _, knowledge, _ = _services(tmp_path)
    saved = knowledge.save("drone_manage", [_glossary(), _analysis_rule()], revision=0)

    assert saved["entries"][0]["terms"][0]["aliases"] == ["飞行器"]
    assert saved["entries"][1]["rule"]["metric"] == "设备数"
    assert saved["entries"][1]["rule"]["checks"][0]["operator"] == "gte"
    assert saved["entries"][1]["rule"]["report_requirements"] == ["注明统计口径"]
    with pytest.raises(MdlConfigError, match="Glossary terms"):
        knowledge.save(
            "drone_manage",
            [_qa() | {"terms": [{"term": "x", "meaning": "y"}]}],
            revision=saved["revision"],
        )
    with pytest.raises(MdlConfigError, match="Analysis rules"):
        knowledge.save(
            "drone_manage",
            [_glossary() | {"rule": {"metric": "x"}}],
            revision=saved["revision"],
        )


def test_duplicate_uuid_spellings_are_rejected_atomically(tmp_path: Path) -> None:
    _, knowledge, _ = _services(tmp_path)
    first = _qa()
    duplicate = _business() | {"id": first["id"].upper()}

    with pytest.raises(MdlConfigError, match="Duplicate"):
        knowledge.save("drone_manage", [first, duplicate], revision=0)
    assert knowledge.load("drone_manage")["entries"] == []


def test_expected_result_validation_and_server_metadata_lifecycle(
    tmp_path: Path,
) -> None:
    _, knowledge, _ = _services(tmp_path)
    entry = _qa() | {
        "expected_result": {"columns": ["ok"], "rows": [[1]]},
        "reuse_on_exact_match": True,
    }
    saved = knowledge.save("drone_manage", [entry], revision=0)
    connector = FakeConnector()
    approved = knowledge.approve(
        "drone_manage",
        entry["id"],
        saved["revision"],
        connector=connector,
        data_source="duckdb",
        reviewed_by="reviewer",
    )
    record = approved["entries"][0]
    assert record["validation"]["mode"] == "result_checked"
    assert record["validation"]["reviewed_by"] == "reviewer"

    reloaded = knowledge.load("drone_manage", include_stale=True)
    assert reloaded["entries"][0]["validation"]["reviewed_by"] == "reviewer"
    unchanged = knowledge.save(
        "drone_manage",
        [record | {"validation": {"forged": True}}],
        revision=approved["revision"],
    )
    assert unchanged["entries"][0]["validation"]["reviewed_by"] == "reviewer"

    edited = knowledge.save(
        "drone_manage",
        [record | {"expected_result": {"columns": ["ok"], "rows": [[2]]}}],
        revision=unchanged["revision"],
    )
    assert edited["entries"][0]["status"] == "draft"
    assert "validation" not in edited["entries"][0]


def test_exact_reuse_requires_opt_in_and_rejects_conflicts(tmp_path: Path) -> None:
    _, knowledge, _ = _services(tmp_path)
    first = _qa() | {"reuse_on_exact_match": True}
    saved = knowledge.save("drone_manage", [first], revision=0)
    approved = knowledge.approve(
        "drone_manage", first["id"], saved["revision"], data_source="duckdb"
    )
    assert (
        knowledge.resolve_exact_sql(
            "drone_manage", "查询无人机数量？", "admin", "admin", entry_id=first["id"]
        )
        == first["sql"]
    )

    second = _qa("55555555-5555-5555-5555-555555555555") | {
        "sql": 'SELECT COUNT(*) AS count FROM "device_registration"',
        "reuse_on_exact_match": True,
    }
    saved2 = knowledge.save(
        "drone_manage",
        [approved["entries"][0], second],
        revision=approved["revision"],
    )
    knowledge.approve(
        "drone_manage",
        second["id"],
        saved2["revision"],
        data_source="duckdb",
    )
    matches = knowledge.search("drone_manage", "查询无人机数量", "admin", "admin")
    assert all(match["conflict"] for match in matches if match["exact_match"])
    with pytest.raises(MdlPolicyError, match="different semantics"):
        knowledge.resolve_exact_sql("drone_manage", "查询无人机数量", "admin", "admin")


def test_executable_rule_roundtrip_and_edit_demotes_approval(tmp_path: Path) -> None:
    _, knowledge, _ = _services(tmp_path)
    rule = _analysis_rule()
    rule["rule"]["checks"] = [
        {
            "column": "count",
            "operator": "gte",
            "value": 0,
            "severity": "error",
            "message": "设备数不能为负",
        }
    ]
    rule["rule"]["report_requirements"] = ["注明去重口径与统计范围"]
    saved = knowledge.save("drone_manage", [rule], revision=0)
    approved = knowledge.approve("drone_manage", rule["id"], saved["revision"])
    loaded = knowledge.load("drone_manage")["entries"][0]
    assert loaded["rule"]["checks"][0]["value"] == 0
    assert loaded["rule"]["report_requirements"] == ["注明去重口径与统计范围"]
    loaded["rule"]["checks"][0]["value"] = 1
    edited = knowledge.save("drone_manage", [loaded], approved["revision"])
    assert edited["entries"][0]["status"] == "draft"


def test_executable_rule_invalid_operator_rejected_before_write(
    tmp_path: Path,
) -> None:
    _, knowledge, _ = _services(tmp_path)
    rule = _analysis_rule()
    rule["rule"]["checks"] = [
        {"column": "count", "operator": "exec", "value": "malicious()"}
    ]
    with pytest.raises(MdlConfigError, match="operator"):
        knowledge.save("drone_manage", [rule], revision=0)
    assert knowledge.load("drone_manage")["entries"] == []


def test_analysis_rule_checks_validate_query_results(tmp_path: Path) -> None:
    _, knowledge, _ = _services(tmp_path)
    rule = _analysis_rule()
    rule["rule"]["checks"] = [
        {"column": "count", "operator": "gte", "value": 1, "message": "设备数至少为 1"}
    ]
    saved = knowledge.save("drone_manage", [rule], revision=0)
    approved = knowledge.approve(
        "drone_manage",
        rule["id"],
        saved["revision"],
        data_source="duckdb",
    )
    record = approved["entries"][0]

    passed = validate_analysis_rules([record], ["count"], [(2,)])
    failed = validate_analysis_rules([record], ["count"], [(0,)])

    assert passed["status"] == "passed"
    assert failed["status"] == "failed"
    assert failed["checks"][0]["failed_rows"] == [1]


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_expected_result_rejects_non_finite_values(tmp_path, value):
    _, knowledge, _ = _services(tmp_path)
    with pytest.raises(MdlConfigError, match="scalar"):
        knowledge.save(
            "drone_manage",
            [_qa() | {"expected_result": {"columns": ["n"], "rows": [[value]]}}],
            0,
        )


def test_glossary_aliases_expand_qa_retrieval(tmp_path: Path) -> None:
    _, knowledge, _ = _services(tmp_path)
    qa = _qa()
    qa["question"] = "查询无人机数量"
    glossary = _glossary()
    glossary["terms"][0]["term"] = "无人机"
    glossary["terms"][0]["aliases"] = ["飞行器"]
    saved = knowledge.save("drone_manage", [qa, glossary], revision=0)
    saved = knowledge.approve(
        "drone_manage",
        qa["id"],
        saved["revision"],
        connector=FakeConnector(),
        data_source="duckdb",
    )
    knowledge.approve(
        "drone_manage",
        glossary["id"],
        saved["revision"],
        connector=FakeConnector(),
        data_source="duckdb",
    )

    matches = knowledge.search(
        "drone_manage",
        "飞行器数量是多少",
        user_id="admin",
        role="admin",
        data_source="duckdb",
        top_k=2,
    )

    assert matches[0]["title"] == "无人机数量"


def test_rule_execution_can_retrieve_all_matches_beyond_prompt_limit(tmp_path):
    import uuid

    _, knowledge, _ = _services(tmp_path)
    entries = [_business() | {"id": str(uuid.uuid4())} for _ in range(21)]
    saved = knowledge.save("drone_manage", entries, 0)
    for entry in entries:
        saved = knowledge.approve("drone_manage", entry["id"], saved["revision"])
    assert (
        len(knowledge.search("drone_manage", "设备状态", "admin", "admin", top_k=None))
        == 21
    )
    assert (
        len(knowledge.search("drone_manage", "设备状态", "admin", "admin", top_k=20))
        == 20
    )


def test_sql_literal_case_and_spaces_are_preserved_in_conflict_detection(tmp_path):
    _, knowledge, _ = _services(tmp_path)
    first = _qa() | {
        "reuse_on_exact_match": True,
        "sql": "SELECT COUNT(*) FROM device_registration WHERE status='Active  OK'",
        "models": {"device_registration": ["status"]},
    }
    second = first | {
        "id": "66666666-6666-6666-6666-666666666666",
        "sql": "SELECT COUNT(*) FROM device_registration WHERE status='active OK'",
    }
    saved = knowledge.save("drone_manage", [first, second], 0)
    for entry in [first, second]:
        saved = knowledge.approve("drone_manage", entry["id"], saved["revision"])
    with pytest.raises(MdlPolicyError, match="different semantics"):
        knowledge.resolve_exact_sql("drone_manage", first["question"], "admin", "admin")
