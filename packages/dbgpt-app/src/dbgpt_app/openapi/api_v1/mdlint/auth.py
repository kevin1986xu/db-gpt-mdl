"""Local identity for the opt-in MDL integration.

Upstream's user-id header is a mock identity. A configured MDL server uses
server-issued tokens instead, including for the existing DB-GPT chat routes.
"""

import hmac
import json
import os
import secrets
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

from starlette.requests import Request
from starlette.responses import JSONResponse

current_principal: ContextVar[Optional[dict]] = ContextVar(
    "mdl_principal", default=None
)
COOKIE = "dbgpt_mdl_session"


def enabled() -> bool:
    return os.getenv("DBGPT_MDL_ENABLED", "0") == "1"


def credentials_path() -> Path:
    return Path(os.getenv("DBGPT_MDL_CREDENTIALS", ".runtime/mdl/credentials.json"))


def initialize_credentials() -> Path:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    if not path.exists():
        credentials = {
            "admin": secrets.token_urlsafe(32),
            "analyst": secrets.token_urlsafe(32),
        }
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return path
        with os.fdopen(fd, "w") as stream:
            json.dump(credentials, stream, indent=2)
    return path


def admin_token() -> Optional[str]:
    """Return the locally generated admin token for trusted auto-login flows."""
    path = initialize_credentials()
    try:
        accounts = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    token = accounts.get("admin") if isinstance(accounts, dict) else None
    return token if isinstance(token, str) else None


def authenticate(token: str) -> Optional[dict]:
    path = credentials_path()
    if not token or not path.exists():
        return None
    accounts = json.loads(path.read_text())
    for username, expected in accounts.items():
        if isinstance(expected, str) and hmac.compare_digest(
            token.encode("utf-8"), expected.encode("utf-8")
        ):
            return {
                "username": username,
                "role": "admin" if username == "admin" else "analyst",
            }
    return None


def error(message: str, status: int):
    return JSONResponse(
        {"success": False, "err_code": str(status), "err_msg": message, "data": None},
        status_code=status,
    )


class MDLIdentityMiddleware:
    """Keep identity alive through streaming responses and copied thread contexts."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not enabled():
            return await self.app(scope, receive, send)
        request = Request(scope)
        authorization = request.headers.get("authorization", "")
        bearer = authorization[7:] if authorization.startswith("Bearer ") else ""
        principal = authenticate(bearer or request.cookies.get(COOKIE, ""))
        path = request.url.path
        public = path in (
            "/api/v1/mdl/login",
            "/api/v1/mdl/session",
            "/api/health",
            "/api/controller/heartbeat",
            "/api/controller/register_instance",
            "/api/controller/deregister_instance",
        ) or path.startswith("/api/controller/")
        if path.startswith("/api/") and not public:
            if not principal:
                return await error("请在语义模型页面登录 MDL 账号", 401)(
                    scope, receive, send
                )
            if request.method not in ("GET", "HEAD", "OPTIONS") and not bearer:
                origin = request.headers.get("origin")
                if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                    return await error("不允许跨站修改请求", 403)(scope, receive, send)
            if principal["role"] != "admin":
                # The analyst profile exposes only governed query operations.
                # Generic file/code/workflow APIs can otherwise bypass SQL policy.
                analyst_paths = {
                    "/api/v1/mdl/schema",
                    "/api/v1/mdl/query",
                    "/api/v1/mdl/knowledge-spaces/search",
                    "/api/v1/mdl/logout",
                    "/api/v1/chat/react-agent",
                    "/api/v1/chat/dialogue/scenes",
                    "/api/v1/chat/mode/params/list",
                    "/api/v1/resource/params/list",
                    "/api/v1/editor/db/tables",
                    "/api/v1/editor/sql/run",
                    "/api/v1/editor/chart/run",
                    "/api/v1/model/types",
                }
                if request.method == "GET" and path == "/api/v2/serve/datasources":
                    analyst_paths.add(path)
                if path not in analyst_paths and not path.startswith(
                    "/api/v1/chat/question/"
                ):
                    return await error("该操作需要管理员权限", 403)(
                        scope, receive, send
                    )
        token = current_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            current_principal.reset(token)
