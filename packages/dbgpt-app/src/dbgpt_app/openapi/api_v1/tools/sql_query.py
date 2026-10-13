"""sql_query tool — read-only SQL query against the selected database."""

import json
from typing import Any, Dict, Optional

from dbgpt.agent.resource.tool.base import tool


def make_sql_query(react_state: Dict[str, Any], database_connector: Optional[Any]):
    @tool(
        description=(
            "对用户选择的数据库执行 SQL 查询（仅支持 SELECT）。"
            '参数: {"sql": "SELECT 语句"}'
        )
    )
    def sql_query(sql: str) -> str:
        """Execute a read-only SQL query against the selected database."""
        react_state.pop("mdl_query_evidence", None)
        if database_connector is None:
            return json.dumps(
                {
                    "chunks": [
                        {
                            "output_type": "text",
                            "content": "未选择数据库，请先在左侧面板选择一个数据源。",
                        }
                    ]
                },
                ensure_ascii=False,
            )

        sql_stripped = sql.strip().rstrip(";")
        reviewed_sql_reused = False
        reviewed_sql_note = ""
        reviewed_entry = None
        if getattr(database_connector, "mdl_enabled", False):
            database = react_state.get("database_name")
            question = react_state.get("user_input")
            principal = react_state.get("mdl_principal") or {}
            if database and isinstance(question, str) and question.strip():
                try:
                    from dbgpt_app.openapi.api_v1.mdlint.errors import MdlPolicyError
                    from dbgpt_app.openapi.api_v1.mdlint.knowledge import (
                        KnowledgeService,
                    )
                    from dbgpt_app.openapi.api_v1.mdlint.service import (
                        infer_wren_data_source,
                    )

                    knowledge_service = KnowledgeService(
                        manifest_path=getattr(
                            getattr(database_connector, "_service", None),
                            "_manifest_path",
                            None,
                        )
                    )
                    reviewed_entry = knowledge_service.resolve_exact_entry(
                        database,
                        question,
                        principal.get("username"),
                        principal.get("role"),
                        data_source=infer_wren_data_source(
                            getattr(
                                database_connector,
                                "_raw_connector",
                                database_connector,
                            )
                        ),
                    )
                    if reviewed_entry:
                        sql_stripped = reviewed_entry["sql"].strip().rstrip(";")
                        reviewed_sql_reused = True
                        reviewed_sql_note = "已复用精确问法对应的审核 SQL。"
                except MdlPolicyError as exc:
                    return json.dumps(
                        {
                            "chunks": [
                                {
                                    "output_type": "text",
                                    "content": (
                                        f"审核 SQL 冲突或不可用，未执行模型 SQL：{exc}"
                                    ),
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                except Exception as exc:
                    return json.dumps(
                        {
                            "chunks": [
                                {
                                    "output_type": "text",
                                    "content": (
                                        f"审核知识库不可用，未执行模型 SQL：{exc}"
                                    ),
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
        sql_upper = sql_stripped.upper().lstrip()
        forbidden = [
            "INSERT",
            "UPDATE",
            "DELETE",
            "DROP",
            "ALTER",
            "TRUNCATE",
            "CREATE",
            "GRANT",
            "REVOKE",
        ]
        for kw in forbidden:
            if sql_upper.startswith(kw):
                return json.dumps(
                    {
                        "chunks": [
                            {
                                "output_type": "text",
                                "content": f"安全限制: 不允许执行 {kw} 语句，"
                                "仅支持 SELECT 查询。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )

        try:
            result = database_connector.run(sql_stripped)
            if not result:
                return json.dumps(
                    {
                        "chunks": [
                            {"output_type": "text", "content": "查询返回空结果。"}
                        ]
                    },
                    ensure_ascii=False,
                )

            columns = result[0]
            col_names = [str(c[0]) if isinstance(c, tuple) else str(c) for c in columns]
            rows = result[1:]
            rule_validation = None
            source_truncated = False
            if getattr(database_connector, "mdl_enabled", False):
                from dbgpt_app.openapi.api_v1.mdlint.service import MDL_MAX_RESULT_ROWS

                source_truncated = len(rows) >= MDL_MAX_RESULT_ROWS
                rule_validation = _validate_matching_analysis_rules(
                    react_state,
                    database_connector,
                    col_names,
                    rows,
                )
                if source_truncated:
                    # Checks on a bounded fetch cannot establish that every
                    # source record passes; failures in the prefix are valid.
                    for check in rule_validation.get("checks", []):
                        if check.get("status") == "passed":
                            check["status"] = "not_applicable"
                            check["message"] += "（达到取数上限，仅校验部分记录）"
                    for rule in rule_validation.get("rules", []):
                        if rule.get("status") == "passed":
                            rule["status"] = "not_applicable"
                    if rule_validation.get("status") == "passed":
                        rule_validation["status"] = "not_applicable"
                react_state["mdl_query_evidence"] = {
                    "sql": sql_stripped,
                    "columns": col_names,
                    "rows": [list(row) for row in rows[:20]],
                    "row_count": len(rows),
                    "truncated": len(rows) > 20,
                    "reviewed_sql_reused": reviewed_sql_reused,
                    "rule_validation": rule_validation,
                }
                if source_truncated:
                    react_state["mdl_query_evidence"]["source_truncated"] = True
                if reviewed_entry:
                    react_state["mdl_query_evidence"]["reviewed_entry"] = {
                        "id": reviewed_entry["id"],
                        "title": reviewed_entry["title"],
                        "validation": reviewed_entry.get("validation"),
                    }

            header = "| " + " | ".join(col_names) + " |"
            separator = "| " + " | ".join(["---"] * len(col_names)) + " |"
            md_rows = []
            for row in rows[:50]:
                md_rows.append("| " + " | ".join(str(v) for v in row) + " |")
            table = "\n".join([header, separator] + md_rows)
            if len(rows) > 50:
                table += f"\n\n（仅显示前 50 行，共 {len(rows)} 行）"
            if source_truncated:
                table += "\n\n（达到取数上限，实际符合查询条件的记录可能更多。）"
            if rule_validation:
                table += "\n\n" + _format_rule_validation(rule_validation)

            # Cap total output size so a single wide query can't blow out the
            # LLM context window. The full result remains available via the
            # ToolResultStorage persistence layer if it exceeds the threshold.
            MAX_SQL_OUTPUT_CHARS = 20_000
            if len(table) > MAX_SQL_OUTPUT_CHARS:
                table = (
                    table[:MAX_SQL_OUTPUT_CHARS]
                    + f"\n\n... [Output truncated at {MAX_SQL_OUTPUT_CHARS} chars. "
                    f"Total rows: {len(rows)}]"
                )

            if reviewed_sql_note:
                table += f"\n\n（{reviewed_sql_note}）"
            return json.dumps(
                {"chunks": [{"output_type": "markdown", "content": table}]},
                ensure_ascii=False,
            )
        except Exception as e:
            react_state.pop("mdl_query_evidence", None)
            return json.dumps(
                {
                    "chunks": [
                        {
                            "output_type": "text",
                            "content": f"SQL 执行失败: {str(e)}",
                        }
                    ]
                },
                ensure_ascii=False,
            )

    return sql_query


def _validate_matching_analysis_rules(
    react_state: Dict[str, Any],
    database_connector: Any,
    columns: list[str],
    rows: list[Any],
) -> dict[str, Any]:
    database = react_state.get("database_name")
    question = react_state.get("user_input")
    principal = react_state.get("mdl_principal") or {}
    if not database or not isinstance(question, str) or not question.strip():
        return {"status": "not_configured", "checks": [], "rules": []}
    from dbgpt_app.openapi.api_v1.mdlint.analysis_rules import validate_analysis_rules
    from dbgpt_app.openapi.api_v1.mdlint.knowledge import KnowledgeService
    from dbgpt_app.openapi.api_v1.mdlint.service import infer_wren_data_source

    knowledge_service = KnowledgeService(
        manifest_path=getattr(
            getattr(database_connector, "_service", None),
            "_manifest_path",
            None,
        )
    )
    matches = knowledge_service.search(
        database,
        question,
        principal.get("username"),
        principal.get("role"),
        top_k=None,
        data_source=infer_wren_data_source(
            getattr(database_connector, "_raw_connector", database_connector)
        ),
    )
    rules = [match for match in matches if match.get("kind") == "analysis_rule"]
    return validate_analysis_rules(rules, columns, rows)


def _format_rule_validation(report: dict[str, Any]) -> str:
    labels = {
        "passed": "通过",
        "failed": "失败",
        "not_applicable": "未能校验",
        "not_configured": "未配置可执行断言",
    }
    lines = [f"业务规则校验：{labels.get(report.get('status'), '未能校验')}。"]
    for check in report.get("checks", []):
        message = check.get("message")
        if not message:
            continue
        status = labels.get(check.get("status"), "未能校验")
        lines.append(f"- {message}：{status}")
    return "\n".join(lines)
