#!/usr/bin/env python
"""Run deterministic MDL knowledge acceptance checks."""

from __future__ import annotations

# ruff: noqa: E402, I001

import argparse
import json
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for package in (
    "dbgpt-app",
    "dbgpt-core",
    "dbgpt-serve",
    "dbgpt-ext",
    "dbgpt-client",
    "dbgpt-sandbox",
):
    sys.path.insert(0, str(ROOT / "packages" / package / "src"))

from dbgpt_app.openapi.api_v1.mdlint.knowledge import KnowledgeService
from dbgpt_app.openapi.api_v1.mdlint.auth import current_principal
from dbgpt_app.openapi.api_v1.mdlint.connector import MdlConnectorProxy
from dbgpt_app.openapi.api_v1.mdlint.query_evidence import ground_final_answer
from dbgpt_app.openapi.api_v1.mdlint.service import MdlQueryService
from dbgpt_app.openapi.api_v1.tools.sql_query import make_sql_query


CASES = [
    {"question": "无人机、遥控器、机场分别对应什么设备类型", "title": "设备分类"},
    {"question": "飞行器属于哪个domain", "title": "设备分类"},
    {"question": "设备SN去重后无人机有几架", "title": "唯一无人机数量"},
    {"question": "不重复的无人机数量是多少", "title": "唯一无人机数量"},
    {"question": "无人机不去重的登记行数", "title": "无人机登记记录数"},
    {"question": "online_status字段的状态码什么意思", "title": "在线状态的解释边界"},
    {"question": "今天的天气怎么样", "title": None},
    {"question": "人员数量是多少", "title": None},
]


class EvaluationConnector:
    db_type = "duckdb"

    def __init__(self, path: Path) -> None:
        self.path = path

    def query_ex(self, sql: str, *args, **kwargs):
        import duckdb

        with duckdb.connect(str(self.path), read_only=True) as connection:
            cursor = connection.execute(sql)
            return [column[0] for column in cursor.description], cursor.fetchall()


def build_demo(
    root: Path,
) -> tuple[KnowledgeService, MdlQueryService, EvaluationConnector]:
    import duckdb

    db_path = root / "synthetic.duckdb"
    with duckdb.connect(str(db_path)) as connection:
        connection.execute(
            "CREATE TABLE raw_devices(device_sn VARCHAR, domain INT, online_status INT)"
        )
        connection.execute(
            "INSERT INTO raw_devices VALUES "
            "('SN-A',0,0),('SN-A',0,0),('SN-B',0,1),(NULL,0,0),"
            "('RC-A',2,0),('DOCK-A',3,0)"
        )
    manifest_path = root / "mdl.json"
    manifest = MdlQueryService(manifest_path=manifest_path)
    manifest.save(
        {
            "database": "evaluation",
            "manifest": {
                "catalog": "wren",
                "schema": "main",
                "models": [
                    {
                        "name": "devices",
                        "tableReference": {"schema": "main", "table": "raw_devices"},
                        "columns": [
                            {"name": "device_sn", "type": "varchar"},
                            {"name": "domain", "type": "integer"},
                            {"name": "online_status", "type": "integer"},
                        ],
                    }
                ],
                "relationships": [],
            },
            "policies": [
                {
                    "username": "analyst",
                    "models": {"devices": {"columns": ["domain"], "filters": []}},
                }
            ],
        },
        revision=0,
    )
    entries = [
        {
            "kind": "business_rule",
            "title": "设备分类",
            "content": "domain=0 为无人机，2 为遥控器，3 为机场。",
            "aliases": ["无人机", "飞行器", "遥控器", "机场", "设备类别"],
            "models": {"devices": ["domain"]},
        },
        {
            "kind": "qa_sql",
            "title": "唯一无人机数量",
            "question": "按设备SN去重后无人机数量是多少",
            "content": "按非空设备SN去重，只统计domain=0的唯一无人机。",
            "sql": "SELECT COUNT(DISTINCT device_sn) AS n FROM devices WHERE domain=0",
            "aliases": ["设备SN去重", "唯一无人机", "不重复的无人机"],
            "models": {"devices": ["device_sn", "domain"]},
            "expected_result": {"columns": ["n"], "rows": [[2]]},
            "reuse_on_exact_match": True,
        },
        {
            "kind": "qa_sql",
            "title": "无人机登记记录数",
            "question": "全部无人机登记记录共有多少",
            "content": "对domain=0逐行计数，保留重复和空SN的登记记录，不去重。",
            "sql": "SELECT COUNT(*) AS n FROM devices WHERE domain=0",
            "aliases": ["登记记录数", "登记行数", "全部登记记录", "不去重"],
            "models": {"devices": ["domain"]},
            "expected_result": {"columns": ["n"], "rows": [[4]]},
        },
        {
            "kind": "business_rule",
            "title": "在线状态的解释边界",
            "content": (
                "online_status为原始状态码，业务含义尚未配置，不能推断在线或离线。"
            ),
            "aliases": ["online_status", "在线状态", "状态码"],
            "models": {"devices": ["online_status"]},
        },
    ]
    for entry in entries:
        entry["id"] = str(uuid.uuid4())
    knowledge = KnowledgeService(manifest_path=manifest_path)
    saved = knowledge.save("evaluation", entries, revision=0)
    connector = EvaluationConnector(db_path)
    for entry in entries:
        saved = knowledge.approve(
            "evaluation",
            entry["id"],
            saved["revision"],
            connector=connector,
            data_source="duckdb",
        )
    return knowledge, manifest, connector


def evaluate(
    knowledge: KnowledgeService,
    manifest: MdlQueryService,
    connector: EvaluationConnector | None,
    *,
    database: str,
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    retrieval = []
    sql_results = []
    seen_sql = set()
    for case in cases:
        matches = knowledge.search(
            database, case["question"], "admin", "admin", top_k=1
        )
        actual = matches[0]["title"] if matches else None
        retrieval.append(
            {
                "question": case["question"],
                "expected_title": case["title"],
                "actual_title": actual,
                "passed": actual == case["title"],
            }
        )
        if connector and matches and matches[0].get("sql"):
            sql_key = (matches[0]["title"], matches[0]["sql"])
            if sql_key in seen_sql:
                continue
            seen_sql.add(sql_key)
            state = _execute_query(
                manifest, connector, database, case["question"], matches[0]["sql"]
            )
            evidence = state.get("mdl_query_evidence") or {}
            columns = evidence.get("columns", [])
            rows = evidence.get("rows", [])
            expected = matches[0].get("expected_result") or {}
            expected_rows = expected.get("rows")
            actual_rows = [list(row) for row in rows]
            passed = columns == expected.get("columns") and actual_rows == expected_rows
            sql_results.append(
                {
                    "title": matches[0]["title"],
                    "columns": columns,
                    "rows": actual_rows,
                    "passed": passed,
                    "grounded_answer_contains_evidence": (
                        "本次查询证据" in ground_final_answer("完成。", state)
                    ),
                }
            )
    if connector:
        qa = next(
            entry
            for entry in knowledge.load(database)["entries"]
            if entry.get("reuse_on_exact_match")
        )
        state = _execute_query(
            manifest, connector, database, qa["question"], "SELECT 999 AS n"
        )
        evidence = state.get("mdl_query_evidence") or {}
        answer = ground_final_answer("共有999架无人机，检查已全部通过。", state)
        sql_results.append(
            {
                "title": "精确问题复用审核 SQL",
                "columns": evidence.get("columns"),
                "rows": evidence.get("rows"),
                "passed": (
                    evidence.get("rows") == qa["expected_result"]["rows"]
                    and evidence.get("reviewed_sql_reused") is True
                    and (evidence.get("reviewed_entry") or {}).get("id") == qa["id"]
                    and "999" not in answer
                ),
                "grounded_answer_contains_evidence": "本次查询证据" in answer,
            }
        )
    policy_checks = [
        {
            "name": "hidden_status_rule_filtered",
            "passed": (
                knowledge.search(database, "在线状态有哪些含义", "analyst", "analyst")
                == []
            ),
        },
        {
            "name": "domain_only_rule_still_visible",
            "passed": bool(
                knowledge.search(database, "遥控器怎么分类", "analyst", "analyst")
            ),
        },
    ]
    return _report(
        database=database,
        retrieval=retrieval,
        sql_results=sql_results,
        policy_checks=policy_checks,
    )


def _execute_query(manifest, connector, database, question, sql):
    principal = {"username": "admin", "role": "admin"}
    token = current_principal.set(principal)
    try:
        proxy = MdlConnectorProxy(connector, database)
        proxy._service = manifest
        state = {
            "database_name": database,
            "user_input": question,
            "mdl_principal": principal,
        }
        make_sql_query(state, proxy)(sql)
        return state
    finally:
        current_principal.reset(token)


def _report(
    *,
    database: str,
    retrieval: list[dict[str, Any]],
    sql_results: list[dict[str, Any]],
    policy_checks: list[dict[str, Any]],
) -> dict[str, Any]:
    sections = {
        "retrieval": retrieval,
        "sql_results": sql_results,
        "policy_checks": policy_checks,
    }
    summary = {
        name: {
            "passed": sum(1 for item in items if item.get("passed")),
            "total": len(items),
        }
        for name, items in sections.items()
    }
    passed = all(part["passed"] == part["total"] for part in summary.values())
    return {
        "database": database,
        "passed": passed,
        "summary": summary,
        **sections,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--manifest", help="Path to mdl.json")
    parser.add_argument("--database", default="evaluation")
    parser.add_argument("--cases", help="Optional JSON case file")
    parser.add_argument("--output", help="Optional JSON output path")
    args = parser.parse_args()

    cases = CASES
    if args.cases:
        cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))

    if args.demo:
        with tempfile.TemporaryDirectory() as temp_dir:
            knowledge, manifest, connector = build_demo(Path(temp_dir))
            report = evaluate(
                knowledge,
                manifest,
                connector,
                database=args.database,
                cases=cases,
            )
    else:
        if not args.manifest:
            parser.error("--manifest is required unless --demo is used")
        manifest = MdlQueryService(manifest_path=args.manifest)
        knowledge = KnowledgeService(manifest_path=args.manifest)
        report = evaluate(
            knowledge,
            manifest,
            None,
            database=args.database,
            cases=cases,
        )

    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
