import json

from dbgpt_app.openapi.api_v1.tools.sql_query import make_sql_query


class FakeMdlConnector:
    mdl_enabled = True

    def run(self, sql: str):
        assert sql == "SELECT count(*) AS count FROM devices"
        return [("count",), (2,)]


def test_mdl_sql_query_records_execution_evidence() -> None:
    state = {}
    sql_query = make_sql_query(state, FakeMdlConnector())

    result = json.loads(sql_query("SELECT count(*) AS count FROM devices"))

    assert result["chunks"][0]["output_type"] == "markdown"
    assert state["mdl_query_evidence"] == {
        "sql": "SELECT count(*) AS count FROM devices",
        "columns": ["count"],
        "rows": [[2]],
        "row_count": 1,
        "truncated": False,
        "reviewed_sql_reused": False,
        "rule_validation": {
            "status": "not_configured",
            "checks": [],
            "rules": [],
        },
    }


def test_mdl_sql_query_clears_old_evidence_on_failed_statement() -> None:
    state = {"mdl_query_evidence": {"columns": ["old"]}}
    sql_query = make_sql_query(state, FakeMdlConnector())

    result = json.loads(sql_query("DELETE FROM devices"))

    assert "mdl_query_evidence" not in state
    assert "不允许执行 DELETE" in result["chunks"][0]["content"]
