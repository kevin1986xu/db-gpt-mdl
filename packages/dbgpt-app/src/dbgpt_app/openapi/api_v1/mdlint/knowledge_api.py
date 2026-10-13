"""HTTP API for datasource-scoped MDL knowledge entries."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .auth import current_principal, enabled
from .knowledge import KnowledgeService
from .service import get_service, infer_wren_data_source

router = APIRouter(prefix="/knowledge")


class KnowledgeEntryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    kind: str
    title: str
    question: str | None = ""
    content: str
    sql: str | None = ""
    aliases: list[str] = Field(default_factory=list)
    models: dict[str, list[str]]
    status: str | None = "draft"
    mdl_fingerprint: str | None = None


class KnowledgeSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: str
    revision: int
    entries: list[KnowledgeEntryRequest]


class KnowledgeApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: str
    id: str
    revision: int


class KnowledgeSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: str
    question: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=4, ge=1, le=20)


def result(data: Any) -> dict[str, Any]:
    return {"success": True, "err_code": None, "err_msg": None, "data": data}


def require_user(admin: bool = False) -> dict[str, Any]:
    if not enabled():
        raise HTTPException(503, "MDL integration is disabled")
    principal = current_principal.get()
    if not principal:
        raise HTTPException(401, "请先登录 MDL 账号")
    if admin and principal["role"] != "admin":
        raise HTTPException(403, "该操作需要管理员权限")
    return principal


def fail(exc: Exception):
    status = 409 if "conflict" in str(exc).lower() else 400
    raise HTTPException(status, str(exc)) from exc


def manager():
    from dbgpt._private.config import Config

    return Config().local_db_manager


def get_knowledge_service() -> KnowledgeService:
    return KnowledgeService(manifest_path=get_service()._manifest_path)


@router.get("")
def get_knowledge(database: str = Query(min_length=1)):
    require_user(admin=True)
    try:
        return result(get_knowledge_service().load(database, include_stale=True))
    except Exception as exc:
        fail(exc)


@router.put("")
def save_knowledge(payload: KnowledgeSaveRequest):
    require_user(admin=True)
    try:
        store = get_knowledge_service().save(
            payload.database,
            [entry.model_dump() for entry in payload.entries],
            payload.revision,
        )
        return result(store)
    except Exception as exc:
        fail(exc)


@router.post("/approve")
def approve_knowledge(payload: KnowledgeApproveRequest):
    require_user(admin=True)
    try:
        raw = manager()._get_raw_connector(payload.database)
        store = get_knowledge_service().approve(
            payload.database,
            payload.id,
            payload.revision,
            connector=raw,
            data_source=infer_wren_data_source(raw),
        )
        return result(store)
    except Exception as exc:
        fail(exc)


@router.post("/search")
def search_knowledge(payload: KnowledgeSearchRequest):
    require_user(admin=True)
    try:
        matches = get_knowledge_service().preview_search(
            payload.database,
            payload.question,
            top_k=payload.top_k,
        )
        return result({"database": payload.database, "matches": matches})
    except Exception as exc:
        fail(exc)
