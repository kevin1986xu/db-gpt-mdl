"""Local MDL runtime setup initialization behavior."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_mdl_local():
    root = Path(__file__).resolve().parents[2]
    path = root / "scripts" / "mdl_local.py"
    spec = importlib.util.spec_from_file_location("mdl_local_for_test", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_initialize_mdl_manifest_copies_example_when_missing(tmp_path) -> None:
    mdl_local = _load_mdl_local()
    example = tmp_path / "mdl.example.json"
    target = tmp_path / "configs" / "mdl.json"
    example.write_text('{"database":"mdl_demo"}\n', encoding="utf-8")

    mdl_local.MDL_EXAMPLE = example
    mdl_local.MDL = target

    mdl_local.initialize_mdl_manifest()

    assert target.read_text(encoding="utf-8") == '{"database":"mdl_demo"}\n'


def test_initialize_mdl_manifest_preserves_existing_config(tmp_path) -> None:
    mdl_local = _load_mdl_local()
    example = tmp_path / "mdl.example.json"
    target = tmp_path / "configs" / "mdl.json"
    example.write_text('{"database":"example"}\n', encoding="utf-8")
    target.parent.mkdir(parents=True)
    target.write_text('{"database":"saved"}\n', encoding="utf-8")

    mdl_local.MDL_EXAMPLE = example
    mdl_local.MDL = target

    mdl_local.initialize_mdl_manifest()

    assert target.read_text(encoding="utf-8") == '{"database":"saved"}\n'
