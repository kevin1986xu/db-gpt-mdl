"""MDL manifest loading."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .errors import MdlConfigError
from .models import MdlManifest

DEFAULT_MDL_PATH = Path("configs/mdl.json")
MDL_PATH_ENV = "DBGPT_MDL_PATH"


def resolve_manifest_path(path: str | Path | None = None) -> Path:
    candidate = path or os.getenv(MDL_PATH_ENV) or DEFAULT_MDL_PATH
    return Path(candidate).expanduser().resolve()


def load_manifest(path: str | Path | None = None) -> MdlManifest:
    manifest_path = resolve_manifest_path(path)
    if not manifest_path.is_file():
        raise MdlConfigError(
            f"MDL manifest not found at {manifest_path}. Set {MDL_PATH_ENV} "
            "or create configs/mdl.json."
        )
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MdlConfigError(f"MDL manifest is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise MdlConfigError(f"Failed to read MDL manifest: {exc}") from exc
    return MdlManifest.from_dict(raw)
