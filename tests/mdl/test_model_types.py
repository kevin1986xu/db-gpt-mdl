"""Model catalog behavior when DB-GPT is running in MDL-only mode."""

import asyncio

from dbgpt_app.openapi.api_v1 import api_v1


def test_model_types_returns_empty_catalog_without_controller():
    result = asyncio.run(api_v1.model_types(None))

    assert result.success is True
    assert result.data == []
