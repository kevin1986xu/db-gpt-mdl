from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import dbgpt_app.openapi.api_v1.mdlint.knowledge_space as knowledge_space_module
from dbgpt_app.openapi.api_v1.mdlint.errors import MdlConfigError, MdlPolicyError
from dbgpt_app.openapi.api_v1.mdlint.knowledge_space import (
    MdlAuthorizedKnowledgeResource,
    MdlKnowledgeSpaceService,
)
from dbgpt_app.openapi.api_v1.mdlint.service import MdlQueryService


def _document(database: str = "drone_manage") -> dict[str, Any]:
    return {
        "database": database,
        "revision": 0,
        "manifest": {
            "schema": "public",
            "models": [
                {
                    "name": "device_registration",
                    "tableReference": {
                        "schema": "public",
                        "table": "device_registration",
                    },
                    "columns": [
                        {"name": "device_code", "type": "varchar"},
                        {"name": "status", "type": "varchar"},
                    ],
                }
            ],
            "relationships": [],
        },
        "policies": [
            {
                "username": "analyst",
                "models": {
                    "device_registration": {
                        "columns": ["device_code"],
                        "filters": [],
                    }
                },
            }
        ],
    }


def _binding() -> dict[str, Any]:
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "title": "设备制度文档",
        "space_name": "device_rules",
        "description": "设备相关制度",
        "models": {"device_registration": ["device_code"]},
        "roles": ["analyst"],
    }


def _service(tmp_path: Path, fingerprint: str = "fp1") -> MdlKnowledgeSpaceService:
    manifest_path = tmp_path / "mdl.json"
    if not manifest_path.exists():
        MdlQueryService(manifest_path=manifest_path).save(_document(), revision=0)
    return MdlKnowledgeSpaceService(
        manifest_path=manifest_path,
        space_resolver=lambda key: SimpleNamespace(name=str(key), id=7),
        fingerprint_provider=lambda space: fingerprint,
    )


def test_binding_lifecycle_authorizes_by_mdl_policy_and_space_fingerprint(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    saved = service.save("drone_manage", [_binding()], revision=0)
    approved = service.approve(
        "drone_manage",
        _binding()["id"],
        saved["revision"],
        reviewed_by="reviewer",
    )

    assert approved["spaces"][0]["status"] == "approved"
    assert approved["spaces"][0]["validation"]["reviewed_by"] == "reviewer"
    assert service.authorized_spaces(
        "drone_manage",
        {"username": "analyst", "role": "analyst"},
    ) == ["device_rules"]
    assert (
        service.authorized_spaces(
            "drone_manage",
            {"username": "other", "role": "analyst"},
        )
        == []
    )

    stale_service = _service(tmp_path, fingerprint="changed")
    assert (
        stale_service.load("drone_manage", include_stale=True)["spaces"][0]["stale"]
        is True
    )
    assert (
        stale_service.authorized_spaces(
            "drone_manage",
            {"username": "analyst", "role": "analyst"},
        )
        == []
    )


def test_hidden_bound_field_blocks_runtime_space_access(tmp_path: Path) -> None:
    service = _service(tmp_path)
    binding = _binding()
    binding["models"] = {"device_registration": ["status"]}
    saved = service.save("drone_manage", [binding], revision=0)
    service.approve("drone_manage", binding["id"], saved["revision"])

    assert (
        service.authorized_spaces(
            "drone_manage",
            {"username": "analyst", "role": "analyst"},
        )
        == []
    )


def test_duplicate_space_and_unknown_bindings_are_rejected(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = _binding()
    second = _binding() | {"id": "22222222-2222-2222-2222-222222222222"}
    with pytest.raises(MdlConfigError, match="Duplicate"):
        service.save("drone_manage", [first, second], revision=0)

    unknown = _binding() | {"models": {"missing": ["field"]}}
    with pytest.raises(MdlConfigError, match="unknown MDL model"):
        service.save("drone_manage", [unknown], revision=0)


def test_explicit_authorization_requires_bound_approved_space(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(MdlPolicyError, match="not bound"):
        service.authorize_space(
            "device_rules",
            database="drone_manage",
            principal={"username": "analyst", "role": "analyst"},
        )


def test_nonexistent_space_is_a_policy_denial_without_registry_details(tmp_path):
    initialized = _service(tmp_path)

    def missing(key):
        raise MdlConfigError("private registry detail: missing space")

    service = MdlKnowledgeSpaceService(
        manifest_path=initialized._manifest_path,
        space_resolver=missing,
    )
    with pytest.raises(MdlPolicyError, match="not bound") as raised:
        service.authorize_space(
            "nonexistent",
            database="drone_manage",
            principal={"username": "analyst", "role": "analyst"},
        )
    assert "private registry" not in str(raised.value)


def test_authorized_resource_rechecks_before_and_after_retrieve() -> None:
    class FlippingService:
        def __init__(self) -> None:
            self.calls = 0

        def authorized_spaces(self, database, principal):
            self.calls += 1
            return ["device_rules"] if self.calls == 1 else []

    class Delegate:
        retriever_name = "device_rules"
        retriever_desc = "rules"

        async def retrieve(self, query):
            return [SimpleNamespace(content="secret", metadata={})]

    resource = MdlAuthorizedKnowledgeResource(
        delegate=Delegate(),
        database="drone_manage",
        principal={"username": "analyst", "role": "analyst"},
        space_name="device_rules",
        service=FlippingService(),
    )

    assert asyncio.run(resource.retrieve("rules")) == []


def test_authorized_resource_uses_lexical_fallback_under_same_authorization(
    monkeypatch,
) -> None:
    class Service:
        def authorized_spaces(self, database, principal):
            return ["device_rules"]

    class BrokenDelegate:
        retriever_name = "device_rules"
        retriever_desc = "rules"

        async def retrieve(self, query):
            raise ConnectionError("embedding backend unavailable")

    monkeypatch.setattr(
        knowledge_space_module,
        "_space_chunk_contents",
        lambda space: [
            {
                "id": 1,
                "document_id": 2,
                "doc_name": "rules.md",
                "content": "设备 状态 口径",
            }
        ],
    )
    resource = MdlAuthorizedKnowledgeResource(
        delegate=BrokenDelegate(),
        database="drone_manage",
        principal={"username": "analyst", "role": "analyst"},
        space_name="device_rules",
        service=Service(),
    )

    chunks = asyncio.run(resource.retrieve("设备口径"))

    assert chunks[0].content == "设备 状态 口径"
    assert chunks[0].metadata["doc_name"] == "rules.md"


def test_metadata_failure_never_falls_back_to_raw_document_content(monkeypatch):
    class Service:
        def authorized_spaces(self, database, principal):
            return ["device_rules"]

    class Delegate:
        async def retrieve(self, query):
            raise ValueError("invalid metadata")

    monkeypatch.setattr(
        knowledge_space_module,
        "_fallback_retrieve_chunks",
        lambda *args: pytest.fail("Metadata failures must not fallback"),
    )
    resource = MdlAuthorizedKnowledgeResource(
        delegate=Delegate(),
        database="drone_manage",
        principal={"username": "analyst", "role": "analyst"},
        space_name="device_rules",
        service=Service(),
    )
    with pytest.raises(ValueError, match="metadata"):
        asyncio.run(resource.retrieve("设备口径"))


@pytest.mark.parametrize(
    "field", ["content", "result", "questions", "doc_type", "doc_token", "chunk_size"]
)
def test_document_retrieval_fields_are_part_of_review_fingerprint(
    tmp_path, monkeypatch, field
):
    initialized = _service(tmp_path)
    document = SimpleNamespace(
        id=1,
        doc_name="rules",
        content="v1",
        result="v1",
        questions="v1",
        doc_type="text",
        doc_token="v1",
        chunk_size=10,
    )
    space = SimpleNamespace(id=1, name="device_rules")

    class Docs:
        def get_knowledge_documents(self, *args, **kwargs):
            return [document] if kwargs["page"] == 1 else []

    class Chunks:
        def get_document_chunks(self, *args, **kwargs):
            return []

    monkeypatch.setattr("dbgpt_serve.rag.models.document_db.KnowledgeDocumentDao", Docs)
    monkeypatch.setattr("dbgpt_serve.rag.models.chunk_db.DocumentChunkDao", Chunks)
    service = MdlKnowledgeSpaceService(
        manifest_path=initialized._manifest_path,
        space_resolver=lambda key: space,
    )
    store = service.save("drone_manage", [_binding()], 0)
    service.approve("drone_manage", _binding()["id"], store["revision"])
    principal = {"username": "analyst", "role": "analyst"}
    assert service.authorized_spaces("drone_manage", principal) == ["device_rules"]
    setattr(document, field, 20 if field == "chunk_size" else "changed")
    assert service.authorized_spaces("drone_manage", principal) == []
    assert (
        service.load("drone_manage", include_stale=True)["spaces"][0]["stale"] is True
    )


@pytest.mark.parametrize("metadata", ["{broken", "[1,2]", 42])
def test_malformed_chunk_metadata_is_rejected(metadata):
    with pytest.raises(MdlPolicyError, match="metadata"):
        knowledge_space_module._parse_chunk_metadata(metadata)
