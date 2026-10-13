"""MDL-specific evidence handling in the sql_query tool."""

import json

from dbgpt_app.openapi.api_v1.tools.sql_query import make_sql_query


class _Connector:
    mdl_enabled = True
    db_type = "duckdb"

    def __init__(self, result):
        self.result = result
        self.ran = []

    def run(self, sql):
        self.ran.append(sql)
        return self.result


def test_mdl_sql_query_records_current_execution_evidence(monkeypatch):
    class _KnowledgeService:
        def __init__(self, *args, **kwargs):
            pass

        def resolve_exact_entry(self, *args, **kwargs):
            return None

        def search(self, *args, **kwargs):
            return [
                {
                    "id": "rule-1",
                    "kind": "analysis_rule",
                    "title": "设备数口径",
                    "rule": {
                        "metric": "设备数",
                        "checks": [
                            {
                                "column": "设备数",
                                "operator": "lt",
                                "value": 3,
                                "severity": "error",
                                "message": "设备数应小于三",
                            }
                        ],
                    },
                }
            ]

    monkeypatch.setattr(
        "dbgpt_app.openapi.api_v1.mdlint.knowledge.KnowledgeService",
        _KnowledgeService,
    )
    state = {
        "database_name": "drone_manage",
        "user_input": "设备数",
        "mdl_principal": {"username": "admin", "role": "admin"},
        "mdl_query_evidence": {"columns": ["stale"], "rows": [[1]]},
    }

    out = make_sql_query(state, _Connector([[("设备数",)], [4]]))(
        sql='SELECT COUNT(*) AS "设备数" FROM device_registration'
    )

    content = json.loads(out)["chunks"][0]["content"]
    assert "业务规则校验：失败" in content
    assert state["mdl_query_evidence"]["columns"] == ["设备数"]
    assert state["mdl_query_evidence"]["rows"] == [[4]]
    assert state["mdl_query_evidence"]["rule_validation"]["status"] == "failed"


def test_mdl_sql_query_fails_closed_when_reviewed_knowledge_is_unavailable(
    monkeypatch,
):
    class _KnowledgeService:
        def __init__(self, *args, **kwargs):
            pass

        def resolve_exact_entry(self, *args, **kwargs):
            raise RuntimeError("broken store")

    monkeypatch.setattr(
        "dbgpt_app.openapi.api_v1.mdlint.knowledge.KnowledgeService",
        _KnowledgeService,
    )
    connector = _Connector([[("设备数",)], [4]])
    state = {
        "database_name": "drone_manage",
        "user_input": "设备数",
        "mdl_principal": {"username": "admin", "role": "admin"},
        "mdl_query_evidence": {"columns": ["stale"], "rows": [[1]]},
    }

    out = make_sql_query(state, connector)(sql="SELECT 1")

    assert "审核知识库不可用" in json.loads(out)["chunks"][0]["content"]
    assert "mdl_query_evidence" not in state
    assert connector.ran == []


def test_bounded_fetch_cannot_claim_all_source_rows_pass(monkeypatch):
    class Knowledge:
        def __init__(self, **kwargs):
            pass

        def resolve_exact_entry(self, *args, **kwargs):
            return None

        def search(self, *args, **kwargs):
            return [
                {
                    "kind": "analysis_rule",
                    "rule": {
                        "checks": [
                            {
                                "column": "n",
                                "operator": "gte",
                                "value": 0,
                                "message": "记录值不能为负",
                                "severity": "error",
                            }
                        ]
                    },
                }
            ]

    monkeypatch.setattr(
        "dbgpt_app.openapi.api_v1.mdlint.knowledge.KnowledgeService", Knowledge
    )
    state = {
        "database_name": "demo",
        "user_input": "记录值",
        "mdl_principal": {"username": "admin", "role": "admin"},
    }
    result = make_sql_query(state, _Connector([["n"], *([[1]] * 1001)]))("SELECT n")
    evidence = state["mdl_query_evidence"]
    assert evidence["source_truncated"] is True
    assert evidence["rule_validation"]["status"] == "not_applicable"
    assert evidence["rule_validation"]["checks"][0]["status"] == "not_applicable"
    assert "达到取数上限" in json.loads(result)["chunks"][0]["content"]


def test_exact_sql_execution_records_reviewed_entry_and_audit_metadata(monkeypatch):
    class Knowledge:
        def __init__(self, **kwargs):
            pass

        def resolve_exact_entry(self, *args, **kwargs):
            return {
                "id": "approved-id",
                "title": "去重口径",
                "sql": "SELECT 2 AS n",
                "validation": {"reviewed_by": "reviewer", "mode": "result_checked"},
            }

        def search(self, *args, **kwargs):
            return []

    monkeypatch.setattr(
        "dbgpt_app.openapi.api_v1.mdlint.knowledge.KnowledgeService", Knowledge
    )
    state = {"database_name": "demo", "user_input": "设备数"}
    connector = _Connector([["n"], [2]])
    make_sql_query(state, connector)("SELECT 999 AS n")
    assert connector.ran == ["SELECT 2 AS n"]
    assert state["mdl_query_evidence"]["reviewed_entry"] == {
        "id": "approved-id",
        "title": "去重口径",
        "validation": {"reviewed_by": "reviewer", "mode": "result_checked"},
    }
