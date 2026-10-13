"""HTTP API for MDL-bound regular knowledge spaces."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .auth import current_principal, enabled
from .knowledge_space import MdlKnowledgeSpaceService
from .service import get_service

router = APIRouter(prefix="/knowledge-spaces")


class KnowledgeSpaceBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    title: str
    space_name: str
    description: str | None = ""
    models: dict[str, list[str]]
    users: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    status: str | None = "draft"
    mdl_fingerprint: str | None = None
    space_fingerprint: str | None = None
    validation: dict[str, Any] | None = None


class KnowledgeSpaceSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: str
    revision: int
    spaces: list[KnowledgeSpaceBindingRequest]


class KnowledgeSpaceApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: str
    id: str
    revision: int


class KnowledgeSpaceSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: str


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


def get_knowledge_space_service() -> MdlKnowledgeSpaceService:
    return MdlKnowledgeSpaceService(manifest_path=get_service()._manifest_path)


@router.get("")
def get_knowledge_spaces(database: str = Query(min_length=1)):
    require_user(admin=True)
    try:
        return result(get_knowledge_space_service().load(database, include_stale=True))
    except Exception as exc:
        fail(exc)


@router.put("")
def save_knowledge_spaces(payload: KnowledgeSpaceSaveRequest):
    require_user(admin=True)
    try:
        store = get_knowledge_space_service().save(
            payload.database,
            [space.model_dump() for space in payload.spaces],
            payload.revision,
        )
        return result(store)
    except Exception as exc:
        fail(exc)


@router.post("/approve")
def approve_knowledge_space(payload: KnowledgeSpaceApproveRequest):
    require_user(admin=True)
    try:
        store = get_knowledge_space_service().approve(
            payload.database,
            payload.id,
            payload.revision,
            reviewed_by=current_principal.get().get("username", "admin"),
        )
        return result(store)
    except Exception as exc:
        fail(exc)


@router.post("/search")
def search_knowledge_spaces(payload: KnowledgeSpaceSearchRequest):
    principal = require_user(admin=False)
    if principal["role"] != "admin":
        protected = os.getenv("DBGPT_MDL_DATABASE", "mdl_demo")
        if payload.database != protected:
            raise HTTPException(403, "该账号只能访问已授权的 MDL 数据源")
    try:
        spaces = get_knowledge_space_service().authorized_spaces(
            payload.database,
            principal,
        )
        return result({"database": payload.database, "spaces": spaces})
    except Exception as exc:
        fail(exc)
