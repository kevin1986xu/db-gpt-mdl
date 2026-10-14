from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dbgpt_app.openapi.api_v1.mdlint import endpoints
from dbgpt_app.openapi.api_v1.mdlint.auth import MDLIdentityMiddleware


def test_local_session_auto_logs_in_admin(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DBGPT_MDL_ENABLED", "1")
    monkeypatch.setenv("DBGPT_MDL_AUTO_LOGIN", "1")
    monkeypatch.setenv("DBGPT_MDL_CREDENTIALS", str(tmp_path / "credentials.json"))

    app = FastAPI()
    app.add_middleware(MDLIdentityMiddleware)
    app.include_router(endpoints.router)

    with TestClient(app) as client:
        response = client.get("/v1/mdl/session")

    assert response.status_code == 200
    assert response.json()["data"]["user"] == {"username": "admin", "role": "admin"}
    assert "dbgpt_mdl_session=" in response.headers["set-cookie"]
