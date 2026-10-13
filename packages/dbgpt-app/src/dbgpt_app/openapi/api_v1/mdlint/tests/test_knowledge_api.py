from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dbgpt_app.openapi.api_v1.mdlint import endpoints, knowledge_api


class FakeKnowledgeService:
    def __init__(self) -> None:
        self.saved: tuple[str, list[dict[str, Any]], int] | None = None
        self.approved: tuple[str, str, int, Any, str] | None = None

    def load(self, database: str, *, include_stale: bool = False):
        return {
            "database": database,
            "revision": 0,
            "entries": [{"id": "entry", "stale": include_stale}],
        }

    def save(self, database: str, entries: list[dict[str, Any]], revision: int):
        self.saved = (database, entries, revision)
        return {"database": database, "revision": revision + 1, "entries": entries}

    def approve(
        self,
        database: str,
        entry_id: str,
        revision: int,
        *,
        connector: Any,
        data_source: str,
    ):
        self.approved = (database, entry_id, revision, connector, data_source)
        return {
            "database": database,
            "revision": revision + 1,
            "entries": [{"id": entry_id, "status": "approved"}],
        }

    def preview_search(self, database: str, question: str, *, top_k: int = 4):
        return [{"id": "entry", "score": 3.0, "question": question, "top_k": top_k}]


class FakeConnector:
    db_type = "duckdb"


class FakeManager:
    def __init__(self) -> None:
        self.connector = FakeConnector()

    def _get_raw_connector(self, database: str):
        return self.connector


def _client(monkeypatch, service: FakeKnowledgeService) -> TestClient:
    monkeypatch.setattr(knowledge_api, "enabled", lambda: True)
    monkeypatch.setattr(knowledge_api, "get_knowledge_service", lambda: service)
    monkeypatch.setattr(knowledge_api, "manager", lambda: FakeManager())
    app = FastAPI()
    app.include_router(endpoints.router)
    return TestClient(app)


def _set_user(role: str | None):
    if role is None:
        return knowledge_api.current_principal.set(None)
    return knowledge_api.current_principal.set({"username": role, "role": role})


def test_knowledge_http_routes_require_admin(monkeypatch) -> None:
    client = _client(monkeypatch, FakeKnowledgeService())

    token = _set_user(None)
    try:
        unauthenticated = client.get(
            "/v1/mdl/knowledge",
            params={"database": "drone_manage"},
        )
    finally:
        knowledge_api.current_principal.reset(token)

    token = _set_user("analyst")
    try:
        analyst = client.get(
            "/v1/mdl/knowledge",
            params={"database": "drone_manage"},
        )
    finally:
        knowledge_api.current_principal.reset(token)

    assert unauthenticated.status_code == 401
    assert analyst.status_code == 403


def test_knowledge_http_crud_approve_and_search(monkeypatch) -> None:
    service = FakeKnowledgeService()
    client = _client(monkeypatch, service)
    token = _set_user("admin")
    try:
        get_response = client.get(
            "/v1/mdl/knowledge",
            params={"database": "drone_manage"},
        )
        save_response = client.put(
            "/v1/mdl/knowledge",
            json={
                "database": "drone_manage",
                "revision": 0,
                "entries": [
                    {
                        "id": "11111111-1111-1111-1111-111111111111",
                        "kind": "qa_sql",
                        "title": "无人机数量",
                        "question": "查询无人机数量",
                        "content": "统计已登记无人机设备编码数量。",
                        "sql": 'SELECT COUNT(device_code) FROM "device_registration"',
                        "aliases": ["飞机数量"],
                        "models": {"device_registration": ["device_code"]},
                        "status": "approved",
                        "mdl_fingerprint": "client-forged",
                    }
                ],
            },
        )
        approve_response = client.post(
            "/v1/mdl/knowledge/approve",
            json={
                "database": "drone_manage",
                "id": "11111111-1111-1111-1111-111111111111",
                "revision": 1,
            },
        )
        search_response = client.post(
            "/v1/mdl/knowledge/search",
            json={
                "database": "drone_manage",
                "question": "飞机数量",
                "top_k": 3,
            },
        )
    finally:
        knowledge_api.current_principal.reset(token)

    assert get_response.status_code == 200
    assert get_response.json()["data"]["entries"][0]["stale"] is True
    assert save_response.status_code == 200
    assert service.saved is not None
    assert service.saved[0] == "drone_manage"
    assert service.saved[1][0]["mdl_fingerprint"] == "client-forged"
    assert approve_response.status_code == 200
    assert service.approved is not None
    assert service.approved[:3] == (
        "drone_manage",
        "11111111-1111-1111-1111-111111111111",
        1,
    )
    assert service.approved[4] == "duckdb"
    assert search_response.status_code == 200
    assert search_response.json()["data"]["matches"][0]["question"] == "飞机数量"
