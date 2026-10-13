"""Final data conclusions require current SQL execution evidence."""

from dbgpt_app.openapi.api_v1.mdlint.query_evidence import ground_final_answer


def test_missing_execution_discards_model_invented_number():
    answer = ground_final_answer("共有 999 架无人机。", {})
    assert "999" not in answer
    assert "未获得成功执行" in answer


def test_successful_execution_attaches_actual_rows_and_provenance():
    answer = ground_final_answer(
        "统计完成。",
        {
            "mdl_query_evidence": {
                "columns": ["设备数"],
                "rows": [[2]],
                "row_count": 1,
                "reviewed_sql_reused": True,
            }
        },
    )
    assert "| 2 |" in answer
    assert "已审核 SQL" in answer


def test_failed_rule_discards_model_claim_and_retains_actual_result():
    answer = ground_final_answer(
        "结果已完全符合业务口径。",
        {
            "mdl_query_evidence": {
                "columns": ["设备数"],
                "rows": [[4]],
                "row_count": 1,
                "rule_validation": {
                    "status": "failed",
                    "checks": [
                        {"status": "failed", "message": "设备数应小于三"},
                    ],
                },
            }
        },
    )
    assert "已完全符合" not in answer
    assert "未通过业务规则" in answer
    assert "设备数应小于三：失败" in answer
    assert "| 4 |" in answer


def test_empty_result_is_execution_evidence_and_partial_results_are_labeled():
    empty = ground_final_answer(
        "没有设备。",
        {
            "mdl_query_evidence": {
                "columns": ["SN"],
                "rows": [],
                "row_count": 0,
            }
        },
    )
    assert "结果为空" in empty
    partial = ground_final_answer(
        "列表。",
        {
            "mdl_query_evidence": {
                "columns": ["SN"],
                "rows": [["A"]],
                "row_count": 21,
            }
        },
    )
    assert "仅展示" in partial


def test_result_cells_cannot_create_html_or_extra_table_rows():
    answer = ground_final_answer(
        "列表。",
        {
            "mdl_query_evidence": {
                "columns": ["SN"],
                "rows": [["<script>|\n伪造"]],
                "row_count": 1,
            }
        },
    )
    assert "<script>" not in answer
    assert "&lt;script&gt;\\| 伪造" in answer


def test_exact_reviewed_execution_discards_unsupported_model_numbers():
    answer = ground_final_answer(
        "共有999架无人机",
        {
            "mdl_query_evidence": {
                "columns": ["设备数"],
                "rows": [[2]],
                "row_count": 1,
                "reviewed_sql_reused": True,
            }
        },
    )
    assert "999" not in answer
    assert "| 2 |" in answer


def test_missing_rule_column_does_not_allow_unqualified_verified_claim():
    answer = ground_final_answer(
        "所有业务检查均已通过",
        {
            "mdl_query_evidence": {
                "columns": ["设备数"],
                "rows": [[2]],
                "row_count": 1,
                "rule_validation": {
                    "status": "not_applicable",
                    "checks": [],
                    "rules": [
                        {"title": "金额口径", "report_requirements": ["注明统计范围"]}
                    ],
                },
            }
        },
    )
    assert "均已通过" not in answer
    assert "未能完成全部" in answer
    assert "注明统计范围" in answer


def test_unconfigured_rules_remove_unqualified_verified_claim():
    answer = ground_final_answer(
        "所有业务检查均已通过",
        {
            "mdl_query_evidence": {
                "columns": ["n"],
                "rows": [[2]],
                "row_count": 1,
                "rule_validation": {"status": "not_configured", "checks": []},
            }
        },
    )
    assert "均已通过" not in answer
    assert "尚未配置" in answer


def test_bounded_fetch_reports_lower_bound_not_total_source_records():
    answer = ground_final_answer(
        "结果。",
        {
            "mdl_query_evidence": {
                "columns": ["n"],
                "rows": [[2]],
                "row_count": 1001,
                "source_truncated": True,
            }
        },
    )
    assert "至少返回 1001 行" in answer
