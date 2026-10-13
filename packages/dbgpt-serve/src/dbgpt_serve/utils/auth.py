import logging
import os
from typing import Optional

from fastapi import Header, HTTPException

from dbgpt._private.pydantic import BaseModel

logger = logging.getLogger(__name__)


class UserRequest(BaseModel):
    user_id: Optional[str] = None
    user_no: Optional[str] = None
    real_name: Optional[str] = None
    # same with user_id
    user_name: Optional[str] = None
    user_channel: Optional[str] = None
    role: Optional[str] = "normal"
    nick_name: Optional[str] = None
    email: Optional[str] = None
    avatar_url: Optional[str] = None
    nick_name_like: Optional[str] = None


def get_user_from_headers(user_id: Optional[str] = Header(None)):
    if os.getenv("DBGPT_MDL_ENABLED") == "1":
        from dbgpt_app.openapi.api_v1.mdlint.auth import current_principal

        principal = current_principal.get()
        if not principal:
            raise HTTPException(status_code=401, detail="MDL login required")
        return UserRequest(
            user_id=principal["username"], role=principal["role"],
            nick_name=principal["username"], real_name=principal["username"],
        )
    try:
        # Mock User Info
        if user_id:
            return UserRequest(
                user_id=user_id, role="admin", nick_name=user_id, real_name=user_id
            )
        else:
            return UserRequest(
                user_id="001", role="admin", nick_name="dbgpt", real_name="dbgpt"
            )
    except Exception as e:
        logging.exception("Authentication failed!")
        raise Exception(f"Authentication failed. {str(e)}")
