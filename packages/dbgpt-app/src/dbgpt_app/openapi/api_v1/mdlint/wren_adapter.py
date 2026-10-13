"""Optional Wren SDK adapter."""

from __future__ import annotations

import base64
import json
from typing import Any

from .errors import MdlWrenUnavailableError
from .models import MdlManifest


class WrenPlanner:
    """Plan semantic SQL through Wren without executing it."""

    def dry_plan(
        self,
        sql: str,
        manifest: MdlManifest,
        data_source: str,
        connection_info: dict[str, Any] | None = None,
    ) -> str:
        try:
            from wren import DataSource, WrenEngine  # type: ignore
            from wren.config import WrenConfig  # type: ignore
        except Exception as exc:
            raise MdlWrenUnavailableError(
                "Wren SDK is not importable. Install the sibling WrenAI package in "
                "the active virtual environment, for example: "
                "pip install -e ../WrenAI/core/wren"
            ) from exc

        try:
            source = DataSource(data_source)
        except Exception as exc:
            raise MdlWrenUnavailableError(
                f"Wren does not support data source {data_source!r}."
            ) from exc

        manifest_json = json.dumps(manifest.to_wren_manifest(), ensure_ascii=False)
        manifest_b64 = base64.b64encode(manifest_json.encode("utf-8")).decode("ascii")
        with WrenEngine(
            manifest_b64,
            source,
            connection_info or {},
            fallback=False,
            config=WrenConfig(strict_mode=True),
        ) as engine:
            return engine.dry_plan(sql)
