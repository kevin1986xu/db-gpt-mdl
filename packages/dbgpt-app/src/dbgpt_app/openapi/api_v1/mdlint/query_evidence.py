"""Ground MDL final answers in the current turn's executed query evidence."""

from __future__ import annotations

from typing import Any


def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")[:500]
    )


def ground_final_answer(content: str, state: dict[str, Any]) -> str:
    """Never retain a model data conclusion without successful turn evidence.

    The caller invokes this only for MDL database conversations. Evidence is
    written by sql_query, never supplied by the user or the language model.
    """
    evidence = state.get("mdl_query_evidence")
    if not isinstance(evidence, dict) or not evidence.get("columns"):
        return (
            "本次未获得成功执行的数据库查询结果，无法给出数据结论。"
            "请根据上方查询错误或缺失的业务口径修正后重试。"
        )
    columns = evidence["columns"]
    rows = evidence.get("rows", [])
    count = evidence.get("row_count", len(rows))
    report = evidence.get("rule_validation") or {}
    reports = report if isinstance(report, list) else [report]
    failed = any(item.get("status") == "failed" for item in reports)
    # Failed business assertions invalidate the model's interpretation, even
    # though the raw SQL execution itself succeeded.
    if failed:
        content = "查询已执行，但结果未通过业务规则校验，暂不能作为符合口径的结论。"
    elif any(item.get("status") == "not_applicable" for item in reports):
        content = "查询已执行，但本次未能完成全部业务断言校验，请结合实际结果核对口径。"
    elif evidence.get("reviewed_sql_reused"):
        # An exact reviewed question has a deterministic result path. Avoid
        # letting the model substitute unsupported numbers for those results.
        content = "已按审核口径完成查询，实际结果如下。"
    elif any(item.get("status") == "not_configured" for item in reports):
        content = "本次查询已执行，尚未配置可执行业务断言，实际结果如下。"
    lines = [content.strip(), "", "本次查询证据：", ""]
    lines.append("| " + " | ".join(_cell(column) for column in columns) + " |")
    lines.append("| " + " | ".join("---" for _ in columns) + " |")
    lines.extend(
        "| " + " | ".join(_cell(value) for value in row) + " |" for row in rows
    )
    if not rows:
        lines.extend(["", "查询结果为空。"])
    if evidence.get("source_truncated"):
        lines.extend(["", f"本次查询至少返回 {count} 行，已达到取数上限。"])
    else:
        lines.extend(["", f"本次查询返回 {count} 行。"])
    if count > len(rows) or evidence.get("truncated"):
        lines.append("以上仅展示查询结果的一部分，不能据此判断未展示的记录。")
    if evidence.get("reviewed_sql_reused"):
        lines.append("执行来源：精确问法对应的已审核 SQL。")
        entry = evidence.get("reviewed_entry") or {}
        if entry.get("title") and entry.get("id"):
            lines.append(
                f"引用口径：{_cell(entry['title'])}（条目 {_cell(entry['id'])}）。"
            )
        validation = entry.get("validation") or {}
        if validation.get("reviewed_by"):
            lines.append(f"审核人：{_cell(validation['reviewed_by'])}。")
        if validation.get("validated_at"):
            lines.append(f"审核时间：{_cell(validation['validated_at'])}。")
    else:
        lines.append("执行来源：本次生成并通过 MDL 权限校验的 SQL。")
    labels = {
        "passed": "通过",
        "failed": "失败",
        "not_applicable": "未能校验",
        "not_configured": "未配置可执行断言",
    }
    for item in reports:
        if not item:
            continue
        status = item.get("status", "not_applicable")
        lines.append(f"业务规则校验：{labels.get(status, '未能校验')}。")
        for check in item.get("checks", item.get("results", [])):
            detail = check.get("message") or check.get("reason") or check.get("title")
            if detail:
                check_status = labels.get(check.get("status"), "未能校验")
                if not lines[-1].startswith("- "):
                    lines.append("")
                lines.append(f"- {_cell(detail)}：{check_status}")
        for rule in item.get("rules", []):
            if rule.get("title"):
                lines.extend(["", f"规则来源：{_cell(rule['title'])}。"])
            requirements = rule.get("report_requirements", [])
            if requirements:
                lines.extend(["报告核对要求：", ""])
                lines.extend(f"- {_cell(requirement)}" for requirement in requirements)
    return "\n".join(lines).strip()
