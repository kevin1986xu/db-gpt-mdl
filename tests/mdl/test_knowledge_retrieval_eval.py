"""Business question regression corpus backed by real Wren and synthetic data."""

import json
import shutil
import uuid

import duckdb
import pytest

from dbgpt_app.openapi.api_v1.mdlint.knowledge import (
    KnowledgeService,
    resolve_knowledge_path,
)
from dbgpt_app.openapi.api_v1.mdlint.service import MdlQueryService


class EvaluationConnector:
    db_type = "duckdb"

    def __init__(self, path):
        self.path = path

    def query_ex(self, sql):
        with duckdb.connect(str(self.path), read_only=True) as connection:
            cursor = connection.execute(sql)
            return [column[0] for column in cursor.description], cursor.fetchall()


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    root = tmp_path_factory.mktemp("knowledge-evaluation")
    raw_path = root / "synthetic.duckdb"
    with duckdb.connect(str(raw_path)) as connection:
        connection.execute(
            "CREATE TABLE raw_devices(device_sn VARCHAR, domain INT, online_status INT)"
        )
        connection.execute(
            "INSERT INTO raw_devices VALUES "
            "('SN-A',0,0),('SN-A',0,0),('SN-B',0,1),(NULL,0,0),"
            "('RC-A',2,0),('DOCK-A',3,0)"
        )
    manifest_path = root / "mdl.json"
    document = {
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
    }
    manifest = MdlQueryService(manifest_path=manifest_path)
    manifest.save(document, revision=0)
    entries = [
        {
            "title": "设备分类",
            "kind": "business_rule",
            "content": "domain=0 为无人机，2 为遥控器，3 为机场。",
            "aliases": ["无人机", "飞行器", "遥控器", "机场", "设备类别"],
            "models": {"devices": ["domain"]},
        },
        {
            "title": "唯一无人机数量",
            "kind": "qa_sql",
            "question": "按设备SN去重后无人机数量是多少",
            "content": "按非空设备SN去重，只统计domain=0的唯一无人机。",
            "sql": "SELECT COUNT(DISTINCT device_sn) AS n FROM devices WHERE domain=0",
            "aliases": ["设备SN去重", "唯一无人机", "不重复的无人机"],
            "models": {"devices": ["device_sn", "domain"]},
        },
        {
            "title": "无人机登记记录数",
            "kind": "qa_sql",
            "question": "全部无人机登记记录共有多少",
            "content": "对domain=0逐行计数，保留重复和空SN的登记记录，不去重。",
            "sql": "SELECT COUNT(*) AS n FROM devices WHERE domain=0",
            "aliases": ["登记记录数", "登记行数", "全部登记记录", "不去重"],
            "models": {"devices": ["domain"]},
        },
        {
            "title": "在线状态的解释边界",
            "kind": "business_rule",
            "content": (
                "online_status为原始状态码，业务含义尚未配置，"
                "不能推断在线或离线。"
            ),
            "aliases": ["online_status", "在线状态", "状态码"],
            "models": {"devices": ["online_status"]},
        },
    ]
    for entry in entries:
        entry["id"] = str(uuid.uuid4())
    knowledge = KnowledgeService(manifest_path=manifest_path)
    saved = knowledge.save("evaluation", entries, revision=0)
    raw = EvaluationConnector(raw_path)
    for entry in entries:
        saved = knowledge.approve(
            "evaluation", entry["id"], saved["revision"], connector=raw
        )
    return knowledge, manifest, raw, document


@pytest.mark.parametrize(
    ("question", "expected_title"),
    [
        ("无人机、遥控器、机场分别对应什么设备类型", "设备分类"),
        ("飞行器属于哪个domain", "设备分类"),
        ("遥控器怎么分类", "设备分类"),
        ("机场设备类别是什么", "设备分类"),
        ("设备SN去重后无人机有几架", "唯一无人机数量"),
        ("不重复的无人机数量是多少", "唯一无人机数量"),
        ("按设备SN计算唯一无人机数量", "唯一无人机数量"),
        ("全部无人机登记记录数", "无人机登记记录数"),
        ("无人机不去重的登记行数", "无人机登记记录数"),
        ("全部无人机登记记录共有多少", "无人机登记记录数"),
        ("online_status字段的状态码什么意思", "在线状态的解释边界"),
        ("在线状态有哪些含义", "在线状态的解释边界"),
        ("今天的天气怎么样", None),
        ("人员数量是多少", None),
        ("订单收入是多少", None),
        ("土地面积是多少", None),
    ],
)
def test_business_questions_retrieve_the_expected_reference(
    corpus, question, expected_title
):
    knowledge, _, _, _ = corpus
    matches = knowledge.search("evaluation", question, "admin", "admin", top_k=1)
    assert [entry["title"] for entry in matches] == (
        [expected_title] if expected_title else []
    )


def test_distinct_and_registration_examples_produce_different_results(corpus):
    knowledge, manifest, raw, _ = corpus
    counts = {}
    for question in ("设备SN去重后无人机有几架", "无人机不去重的登记行数"):
        entry = knowledge.search("evaluation", question, "admin", "admin", top_k=1)[0]
        planned = manifest.plan_authorized_sql(
            entry["sql"], "admin", data_source="duckdb", role="admin"
        )
        counts[entry["title"]] = raw.query_ex(planned.planned_sql)[1][0][0]
    assert counts == {"唯一无人机数量": 2, "无人机登记记录数": 4}


def test_hidden_bindings_filter_otherwise_relevant_questions(corpus):
    knowledge, _, _, _ = corpus
    assert (
        knowledge.search("evaluation", "online_status状态码", "analyst", "analyst")
        == []
    )
    assert not any(
        entry["title"] == "唯一无人机数量"
        for entry in knowledge.search(
            "evaluation", "按设备SN计算唯一无人机数量", "analyst", "analyst"
        )
    )
    assert knowledge.search("evaluation", "遥控器怎么分类", "analyst", "analyst")


def test_corpus_is_not_retrieved_from_another_database(corpus, tmp_path):
    knowledge, _, _, document = corpus
    path = tmp_path / "mdl.json"
    other = {**document, "database": "other", "revision": 1}
    path.write_text(json.dumps(other), encoding="utf-8")
    shutil.copytree(
        resolve_knowledge_path(knowledge._manifest_path, "evaluation").parent,
        resolve_knowledge_path(path, "other").parent,
    )
    isolated = KnowledgeService(manifest_path=path)
    assert isolated.search("other", "唯一无人机数量", "admin", "admin") == []
