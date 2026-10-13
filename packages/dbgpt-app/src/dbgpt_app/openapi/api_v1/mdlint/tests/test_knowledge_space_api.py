from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dbgpt_app.openapi.api_v1.mdlint import endpoints, knowledge_space_api


class FakeKnowledgeSpaceService:
    def __init__(self) -> None:
        self.saved: tuple[str, list[dict[str, Any]], int] | None = None
        self.approved: tuple[str, str, int, str] | None = None

    def load(self, database: str, *, include_stale: bool = False):
        return {
            "database": database,
            "revision": 0,
            "spaces": [{"id": "space", "stale": include_stale}],
        }

    def save(self, database: str, spaces: list[dict[str, Any]], revision: int):
        self.saved = (database, spaces, revision)
        return {"database": database, "revision": revision + 1, "spaces": spaces}

    def approve(
        self,
        database: str,
        binding_id: str,
        revision: int,
        *,
        reviewed_by: str = "admin",
    ):
        self.approved = (database, binding_id, revision, reviewed_by)
        return {
            "database": database,
            "revision": revision + 1,
            "spaces": [{"id": binding_id, "status": "approved"}],
        }

    def authorized_spaces(self, database: str, principal: dict[str, Any]):
        return ["device_rules"]


def _client(monkeypatch, service: FakeKnowledgeSpaceService) -> TestClient:
    monkeypatch.setattr(knowledge_space_api, "enabled", lambda: True)
    monkeypatch.setattr(
        knowledge_space_api,
        "get_knowledge_space_service",
        lambda: service,
    )
    app = FastAPI()
    app.include_router(endpoints.router)
    return TestClient(app)


def _set_user(role: str | None):
    if role is None:
        return knowledge_space_api.current_principal.set(None)
    return knowledge_space_api.current_principal.set({"username": role, "role": role})


def test_knowledge_space_http_routes_require_admin(monkeypatch) -> None:
    client = _client(monkeypatch, FakeKnowledgeSpaceService())

    token = _set_user("analyst")
    try:
        response = client.get(
            "/v1/mdl/knowledge-spaces",
            params={"database": "drone_manage"},
        )
    finally:
        knowledge_space_api.current_principal.reset(token)

    assert response.status_code == 403


def test_knowledge_space_http_crud_and_search(monkeypatch) -> None:
    service = FakeKnowledgeSpaceService()
    client = _client(monkeypatch, service)
    token = _set_user("admin")
    try:
        get_response = client.get(
            "/v1/mdl/knowledge-spaces",
            params={"database": "drone_manage"},
        )
        save_response = client.put(
            "/v1/mdl/knowledge-spaces",
            json={
                "database": "drone_manage",
                "revision": 0,
                "spaces": [
                    {
                        "id": "11111111-1111-1111-1111-111111111111",
                        "title": "设备制度",
                        "space_name": "device_rules",
                        "models": {"device_registration": ["device_code"]},
                    }
                ],
            },
        )
        approve_response = client.post(
            "/v1/mdl/knowledge-spaces/approve",
            json={
                "database": "drone_manage",
                "id": "11111111-1111-1111-1111-111111111111",
                "revision": 1,
            },
        )
    finally:
        knowledge_space_api.current_principal.reset(token)

    assert get_response.status_code == 200
    assert get_response.json()["data"]["spaces"][0]["stale"] is True
    assert save_response.status_code == 200
    assert service.saved[0] == "drone_manage"
    assert approve_response.status_code == 200
    assert service.approved == (
        "drone_manage",
        "11111111-1111-1111-1111-111111111111",
        1,
        "admin",
    )

    monkeypatch.setenv("DBGPT_MDL_DATABASE", "drone_manage")
    token = _set_user("analyst")
    try:
        search_response = client.post(
            "/v1/mdl/knowledge-spaces/search",
            json={"database": "drone_manage"},
        )
        forbidden = client.post(
            "/v1/mdl/knowledge-spaces/search",
            json={"database": "other"},
        )
    finally:
        knowledge_space_api.current_principal.reset(token)

    assert search_response.status_code == 200
    assert search_response.json()["data"]["spaces"] == ["device_rules"]
    assert forbidden.status_code == 403
