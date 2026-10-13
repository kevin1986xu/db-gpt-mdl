from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dbgpt_app.openapi.api_v1.mdlint import (
    MdlConfigError,
    MdlQueryService,
    MdlWrenUnavailableError,
    endpoints,
)
from dbgpt_app.openapi.api_v1.mdlint.models import MdlManifest
from dbgpt_app.openapi.api_v1.mdlint.service import _database_snapshot_path
from dbgpt_app.openapi.api_v1.mdlint.wren_adapter import WrenPlanner


def _document(
    database: str,
    model_name: str,
    display_name: str,
    description: str,
) -> dict:
    return {
        "database": database,
        "revision": 0,
        "manifest": {
            "schema": "public",
            "models": [
                {
                    "name": model_name,
                    "tableReference": {"schema": "public", "table": model_name},
                    "columns": [
                        {
                            "name": "id",
                            "type": "integer",
                            "display_name": display_name,
                            "description": description,
                        }
                    ],
                }
            ],
            "relationships": [],
        },
        "policies": [
            {
                "username": f"{database}_analyst",
                "models": {model_name: {"columns": ["id"], "filters": []}},
            }
        ],
    }


def _first_column(document: dict) -> dict:
    return document["manifest"]["models"][0]["columns"][0]


def test_database_documents_are_isolated_across_switches(tmp_path: Path) -> None:
    manifest_path = tmp_path / "mdl.json"
    service = MdlQueryService(manifest_path=manifest_path)

    alpha = service.save(
        _document("alpha", "alpha_device", "Alpha ID", "Alpha description"),
        revision=0,
    )
    beta = service.save(
        _document("beta", "beta_device", "Beta ID", "Beta description"),
        revision=0,
    )
    updated_alpha_document = service.load("alpha")
    _first_column(updated_alpha_document)["display_name"] = "Alpha ID saved again"
    updated_alpha = service.save(updated_alpha_document, revision=alpha.revision)

    alpha_loaded = service.load("alpha")
    beta_loaded = service.load("beta")

    assert alpha.revision == 1
    assert beta.revision == 1
    assert updated_alpha.revision == 2
    assert alpha_loaded["database"] == "alpha"
    assert beta_loaded["database"] == "beta"
    assert alpha_loaded["manifest"]["models"][0]["name"] == "alpha_device"
    assert beta_loaded["manifest"]["models"][0]["name"] == "beta_device"
    assert _first_column(alpha_loaded)["display_name"] == "Alpha ID saved again"
    assert _first_column(beta_loaded)["display_name"] == "Beta ID"
    assert alpha_loaded["policies"][0]["username"] == "alpha_analyst"
    assert beta_loaded["policies"][0]["username"] == "beta_analyst"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["database"] == "alpha"


def test_unsaved_database_returns_empty_draft_instead_of_active_document(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "mdl.json"
    service = MdlQueryService(manifest_path=manifest_path)
    service.save(
        _document("alpha", "alpha_device", "Alpha ID", "Alpha description"),
        revision=0,
    )

    draft = service.load("beta")

    assert draft == {
        "manifest": {
            "catalog": "wren",
            "schema": "public",
            "models": [],
            "relationships": [],
        },
        "policies": [],
        "revision": 0,
        "database": "beta",
    }


def test_empty_saved_database_document_does_not_fall_back_to_active_document(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "mdl.json"
    service = MdlQueryService(manifest_path=manifest_path)
    service.save(
        _document("alpha", "alpha_device", "Alpha ID", "Alpha description"),
        revision=0,
    )
    empty_beta = {
        "database": "beta",
        "revision": 0,
        "manifest": {"models": [], "relationships": []},
        "policies": [],
    }
    _database_snapshot_path(manifest_path.resolve(), "beta").parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    _database_snapshot_path(manifest_path.resolve(), "beta").write_text(
        json.dumps(empty_beta),
        encoding="utf-8",
    )

    draft = service.load("beta")

    assert draft["database"] == "beta"
    assert draft["revision"] == 0
    assert draft["manifest"]["catalog"] == "wren"
    assert draft["manifest"]["models"] == []
    assert service.load("alpha")["manifest"]["models"][0]["name"] == "alpha_device"


def test_stale_revision_conflict_does_not_overwrite_other_database(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "mdl.json"
    service = MdlQueryService(manifest_path=manifest_path)
    alpha = service.save(
        _document("alpha", "alpha_device", "Alpha ID", "Alpha description"),
        revision=0,
    )
    service.save(
        _document("beta", "beta_device", "Beta ID", "Beta description"),
        revision=0,
    )
    winning_alpha = service.load("alpha")
    _first_column(winning_alpha)["description"] = "Winning alpha description"
    service.save(winning_alpha, revision=alpha.revision)
    stale_alpha = service.load("alpha")
    _first_column(stale_alpha)["description"] = "Stale alpha description"

    with pytest.raises(MdlConfigError, match="revision conflict"):
        service.save(stale_alpha, revision=alpha.revision)

    assert _first_column(service.load("alpha"))["description"] == (
        "Winning alpha description"
    )
    assert _first_column(service.load("beta"))["description"] == "Beta description"


def test_save_policies_rejects_when_active_database_changed(tmp_path: Path) -> None:
    manifest_path = tmp_path / "mdl.json"
    service = MdlQueryService(manifest_path=manifest_path)
    alpha = service.save(
        _document("alpha", "alpha_device", "Alpha ID", "Alpha description"),
        revision=0,
    )
    service.save(
        _document("beta", "beta_device", "Beta ID", "Beta description"),
        revision=0,
    )

    with pytest.raises(MdlConfigError, match="active database conflict"):
        service.save_policies(
            [
                {
                    "username": "alpha_stale",
                    "models": {"alpha_device": {"columns": ["id"], "filters": []}},
                }
            ],
            revision=alpha.revision,
            database="alpha",
        )

    assert service.load("alpha")["policies"][0]["username"] == "alpha_analyst"
    assert service.load("beta")["policies"][0]["username"] == "beta_analyst"


def test_arbitrary_database_name_uses_hashed_snapshot_path(tmp_path: Path) -> None:
    manifest_path = tmp_path / "mdl.json"
    service = MdlQueryService(manifest_path=manifest_path)
    database = "../outside/../../tenant?name=beta"

    service.save(
        _document(database, "tenant_device", "Tenant ID", "Tenant description"),
        revision=0,
    )

    snapshot_path = _database_snapshot_path(manifest_path.resolve(), database)
    assert snapshot_path.parent == tmp_path / "mdl.databases"
    assert snapshot_path.name.endswith(".json")
    assert "/" not in snapshot_path.name
    assert ".." not in snapshot_path.name
    assert service.load(database)["database"] == database


def test_database_snapshot_mismatch_is_rejected(tmp_path: Path) -> None:
    manifest_path = tmp_path / "mdl.json"
    snapshot_path = _database_snapshot_path(manifest_path.resolve(), "beta")
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "database": "alpha",
                "revision": 1,
                "manifest": {"models": [], "relationships": []},
                "policies": [],
            }
        ),
        encoding="utf-8",
    )
    service = MdlQueryService(manifest_path=manifest_path)

    with pytest.raises(MdlConfigError, match="snapshot mismatch"):
        service.load("beta")


def test_http_routes_read_and_save_database_specific_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "mdl.json"
    service = MdlQueryService(manifest_path=manifest_path)
    service.save(
        _document("alpha", "alpha_device", "Alpha ID", "Alpha description"),
        revision=0,
    )

    monkeypatch.setenv("DBGPT_MDL_DATABASE", "mdl_demo")
    monkeypatch.setattr(endpoints, "get_service", lambda: service)
    monkeypatch.setattr(
        endpoints,
        "require_user",
        lambda admin=False: {"username": "admin", "role": "admin"},
    )
    monkeypatch.setattr(
        endpoints,
        "validate_on_database",
        lambda document, database=None: None,
    )

    app = FastAPI()
    app.include_router(endpoints.router)
    client = TestClient(app)

    active_response = client.get("/v1/mdl")
    beta_response = client.get("/v1/mdl", params={"database": "beta"})
    save_response = client.put(
        "/v1/mdl",
        json={
            "database": "beta",
            "revision": 0,
            "manifest": _document(
                "beta",
                "beta_device",
                "Beta ID",
                "Beta description",
            )["manifest"],
        },
    )
    alpha_response = client.get("/v1/mdl", params={"database": "alpha"})
    beta_saved_response = client.get("/v1/mdl", params={"database": "beta"})
    stale_policy_response = client.put(
        "/v1/mdl/policies",
        json={
            "database": "alpha",
            "revision": 1,
            "policies": [
                {
                    "username": "alpha_stale",
                    "models": {"alpha_device": {"columns": ["id"], "filters": []}},
                }
            ],
        },
    )
    monkeypatch.setattr(
        endpoints,
        "manager",
        lambda: (_ for _ in ()).throw(AssertionError("manager called")),
    )
    wrong_database_query_response = client.post(
        "/v1/mdl/query",
        json={"database": "alpha", "sql": "SELECT 1"},
    )

    assert active_response.status_code == 200
    assert beta_response.status_code == 200
    assert save_response.status_code == 200
    assert alpha_response.status_code == 200
    assert beta_saved_response.status_code == 200
    assert stale_policy_response.status_code == 409
    assert wrong_database_query_response.status_code == 400
    assert wrong_database_query_response.json()["detail"] == (
        "请先保存所选数据源的MDL，使其生效后再验证SQL"
    )
    assert active_response.json()["data"]["database"] == "alpha"
    assert beta_response.json()["data"]["database"] == "beta"
    assert beta_response.json()["data"]["revision"] == 0
    assert beta_response.json()["data"]["manifest"]["models"] == []
    assert save_response.json()["data"]["database"] == "beta"
    assert save_response.json()["data"]["revision"] == 1
    assert alpha_response.json()["data"]["manifest"]["models"][0]["name"] == (
        "alpha_device"
    )
    assert beta_saved_response.json()["data"]["manifest"]["models"][0]["name"] == (
        "beta_device"
    )
    assert service.load("alpha")["policies"][0]["username"] == "alpha_analyst"
    assert service.load("beta")["policies"] == []


def test_empty_database_draft_supports_manual_model_wren_dry_plan(
    tmp_path: Path,
) -> None:
    service = MdlQueryService(manifest_path=tmp_path / "mdl.json")
    draft = service.load("manual")
    draft["manifest"]["models"] = [
        {
            "name": "manual_device",
            "tableReference": {"schema": "public", "table": "manual_device"},
            "columns": [{"name": "id", "type": "integer"}],
        }
    ]
    draft["revision"] = 1
    manifest = MdlManifest.from_dict(draft)

    try:
        planned = WrenPlanner().dry_plan(
            'SELECT id FROM "manual_device" LIMIT 0',
            manifest,
            "duckdb",
        )
    except MdlWrenUnavailableError as exc:
        pytest.skip(str(exc))

    assert "manual_device" in planned
