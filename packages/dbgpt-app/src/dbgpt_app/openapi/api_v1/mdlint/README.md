# DB-GPT Wren MDL integration

This package is the backend boundary for routing DB-GPT SQL execution through
Wren MDL before a database connector is called.

The manifest path is resolved from `DBGPT_MDL_PATH`, falling back to
`configs/mdl.json`. The preferred file shape is:

```json
{
  "database": "mdl_demo",
  "revision": 1,
  "manifest": {
    "models": [
      {
        "name": "orders",
        "columns": [{"name": "id"}, {"name": "customer_id"}, {"name": "amount"}]
      }
    ],
    "relationships": []
  },
  "policies": {
    "default": {
      "models": {
        "orders": {
          "columns": ["id", "customer_id"],
          "row_filter": "customer_id = 1001"
        }
      }
    },
    "users": {
      "admin": {
        "models": {
          "orders": {"columns": ["id", "customer_id", "amount"]}
        }
      }
    }
  }
}
```

Pure Wren MDL JSON with a top-level `policies` object is also accepted for
backward compatibility.

The central DB-GPT datasource hook should call `wrap_connector(connector,
db_name)`. The wrapper activates only for `DBGPT_MDL_DATABASE` (default:
`mdl_demo`) and only when `DBGPT_MDL_ENABLED=1`. A manifest file alone does not
enable the integration.

Protected connector calls read the authenticated identity from
`mdlint.auth.current_principal`, a `ContextVar` owned by the auth/middleware
layer. The proxy captures that principal when `wrap_connector(...)` creates the
request-scoped wrapper, exposes `mdl_enabled = True`, and reloads manifest policy
on each method call. Missing identity defaults to deny. The wrapper never falls
back to raw SQL when Wren is unavailable; it raises `MdlWrenUnavailableError`.

Endpoint code can use `get_service()` or `MDLService`/`MdlQueryService`:

- `load(database=None)` returns `{manifest, revision, policies, database}` for
  the active or selected datasource. An unsaved datasource returns an empty
  draft. Saved datasource documents are retained under `mdl.databases/` beside
  the active manifest.
- `get()` returns the Wren manifest plus local policies.
- `save(manifest, revision=None)` validates and writes `DBGPT_MDL_PATH` or
  `configs/mdl.json`, incrementing `revision`.
- `validate(manifest=None)` validates a supplied manifest or the configured file.
- `save_policies(policies, revision=None)` updates only the local policy block.
- `schema(user_id)` returns the policy-visible model/column map.
- `policies()` returns the manifest `policies` object.
- `effective_manifest(user_id)` returns a Wren manifest filtered to visible models.
- `map_data_source(connector)` maps DB-GPT dialects such as `sqlite`/`duckdb`,
  `postgresql`, and `mysql` to Wren data source names. `sqlite` maps to Wren
  `duckdb` for the local demo path.
