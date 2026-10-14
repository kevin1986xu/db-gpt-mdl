# DB-GPT MDL local runtime

This project runs the modified DB-GPT source tree directly through `PYTHONPATH`
and routes protected datasource access through Wren MDL. Without model credentials,
the local server supports MDL editing and governed SQL validation. With credentials,
the web process also starts an OpenAI-compatible LLM worker and embedded controller.

## Setup

Use Python 3.12 and Node.js 20. The Wren SDK is installed from a sibling checkout
because `requirements-mdl.txt` references `../WrenAI/core/wren`.

```bash
git clone https://github.com/Canner/WrenAI.git WrenAI
git -C WrenAI checkout fedf182b4bb8294623d9cc0a9db4fbae1edf63fb
git clone -b dev https://github.com/kevin1986xu/db-gpt-mdl.git
cd db-gpt-mdl
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-mdl.txt
.venv/bin/python scripts/mdl_local.py setup
```

Build the integrated DB-GPT web bundle once before starting the server:

```bash
cd web
npm ci --legacy-peer-deps --ignore-scripts
npm run compile
cd ..
```

`setup` creates:

- `configs/mdl.json` from the versioned `configs/mdl.example.json` when no local
  document exists. Existing saved models are preserved. Local MDL documents and
  datasource snapshots are ignored by Git.
- `.runtime/mdl/demo.duckdb` with `raw_orders`, `raw_customers`, and
  `secret_payroll`.
- `.runtime/mdl/dbgpt.db` with a `connect_config` row for `mdl_demo`.
- `.runtime/mdl/credentials.json` with local `admin` and `analyst` tokens.

## Start

```bash
.venv/bin/python scripts/mdl_local.py start --wait 60
```

Open DB-GPT at `http://127.0.0.1:18765`. The helper starts the full DB-GPT
web process from the checked-out source tree. The local profile configures
`deepseek/deepseek-v4.1-flash` through `https://api.hzlinkapi.com/v1`.
The gateway accepts `deepseek-v4.1-flash` as its request model ID, so the profile
uses DB-GPT's `backend` mapping while preserving the model name shown in the UI.
The local helper automatically opens the MDL workbench as the generated `admin`
account; no token entry is required. Set `DBGPT_MDL_AUTO_LOGIN=0` before starting
if manual MDL login is needed.

The API key is loaded from `OPENAI_API_KEY` or the `openai_api_key` entry in
`.runtime/mdl/model_credentials.json`. Keep this file at mode `0600`; `.runtime/`
is ignored by Git. Restart the service after changing model configuration or
credentials. No embedding model is required for the local MDL workflow.

Administrators can manage datasource-specific reviewed SQL examples and business
rules in the MDL workbench. See [MDL knowledge](MDL_KNOWLEDGE.md) for the approval
workflow, retrieval behavior, and local storage boundary.

Useful commands:

```bash
.venv/bin/python scripts/mdl_local.py status
.venv/bin/python scripts/mdl_local.py tail -n 120
.venv/bin/python scripts/mdl_local.py stop
.venv/bin/python scripts/mdl_local.py smoke
```

Run the source tests with all DB-GPT packages visible:

```bash
.venv/bin/python -m pip install pytest ruff
PYTHONPATH=packages/dbgpt-app/src:packages/dbgpt-core/src:packages/dbgpt-ext/src:packages/dbgpt-serve/src:packages/dbgpt-client/src:packages/dbgpt-sandbox/src \
  .venv/bin/python -m pytest tests/mdl packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/mdlint/tests -q
```

## Integration Boundary

When `DBGPT_MDL_ENABLED=1`, DB-GPT's `ConnectorManager.get_connector()` wraps
the configured `DBGPT_MDL_DATABASE` connector. The wrapper plans SQL through
Wren using `configs/mdl.json` before calling the underlying DB-GPT connector.

The MDL workbench loads saved models for the selected datasource through
`GET /api/v1/mdl?database=<name>`. Refresh keeps that selection. A datasource
without saved MDL starts with an empty model list; physical tables are available
only as candidates for manually adding a model.

Saving retains a separate document and revision for each datasource under
`configs/mdl.databases/`, using hashed filenames. The latest saved datasource
also remains in `configs/mdl.json` as the active document used by chat and query
execution. Existing single-document configurations are read without migration;
switching the workbench selection alone does not change the active datasource.
SQL validation rejects a selection that differs from the active datasource;
save the selected MDL first to activate it for validation.

The demo intentionally includes `secret_payroll`; it is not present in the MDL,
so user queries against it should fail before raw SQL reaches DuckDB.
