"""Connector wrapper that enforces MDL planning and policy centrally."""

from __future__ import annotations

import os
from typing import Any

from .errors import MdlPolicyError
from .service import (
    MDL_QUERY_TIMEOUT_SECONDS,
    MdlQueryService,
    bounded_query_sql,
    infer_wren_data_source,
)

MDL_DATABASE_ENV = "DBGPT_MDL_DATABASE"
MDL_ENABLED_ENV = "DBGPT_MDL_ENABLED"
DEFAULT_MDL_DATABASE = "mdl_demo"
CURRENT_PRINCIPAL_CONTEXT_NAME = "current_principal"


def wrap_connector(connector: Any, db_name: str) -> Any:
    """Wrap the target DB-GPT connector when MDL enforcement is configured."""
    if db_name != os.getenv(MDL_DATABASE_ENV, DEFAULT_MDL_DATABASE):
        return connector
    if not _should_enable_mdl():
        return connector
    if isinstance(connector, MdlConnectorProxy):
        return connector
    return MdlConnectorProxy(connector, db_name)


class MdlConnectorProxy:
    """A DB-GPT connector facade backed by Wren MDL policy checks."""

    mdl_enabled = True

    def __init__(self, raw_connector: Any, db_name: str):
        self._raw_connector = raw_connector
        self._db_name = db_name
        self._service = MdlQueryService.from_env()
        self._principal = _require_current_principal()

    @property
    def raw_connector(self) -> Any:
        """Private escape hatch for admin catalog/config endpoints."""
        if self._principal.get("role") != "admin":
            raise MdlPolicyError("Only admin principals can access the raw connector.")
        return self._raw_connector

    @property
    def db_type(self) -> str:
        return getattr(self._raw_connector, "db_type", "")

    @property
    def dialect(self) -> str:
        dialect = getattr(self._raw_connector, "dialect", None)
        if dialect:
            return str(dialect)
        db_type = getattr(self._raw_connector, "db_type", None)
        if db_type:
            return str(db_type)
        return infer_wren_data_source(self._raw_connector)

    @property
    def db_url(self) -> str:
        raise MdlPolicyError("MDL protected connector does not expose raw db_url.")

    @property
    def table_info(self) -> str:
        return self.get_table_info()

    def get_table_names(self):
        return list(self._visible_schema().keys())

    def get_usable_table_names(self):
        return self.get_table_names()

    def get_table_info(self, table_names: list[str] | None = None) -> str:
        schema = self._visible_schema()
        names = table_names or list(schema.keys())
        missing = set(names) - set(schema)
        if missing:
            raise MdlPolicyError(
                f"Tables are not visible through MDL policy: {sorted(missing)}"
            )
        table_defs = []
        for name in names:
            columns = ", ".join(
                f"{column['name']} {column.get('type') or 'TEXT'}"
                for column in schema[name]
            )
            table_defs.append(f"CREATE TABLE {name} ({columns});")
        return "\n\n".join(table_defs)

    def get_table_info_no_throw(self, table_names: list[str] | None = None) -> str:
        try:
            return self.get_table_info(table_names=table_names)
        except Exception:
            return ""

    def table_simple_info(self):
        schema = self._visible_schema()
        return [
            f"{name}({','.join(column['name'] for column in columns)});"
            for name, columns in schema.items()
        ]

    def run(self, command: str, fetch: str = "all") -> list:
        columns, rows = self.query_ex(command, fetch=fetch)
        header = tuple(columns)
        return [header] + list(rows or [])

    def run_to_df(self, command: str, fetch: str = "all"):
        import pandas as pd

        columns, rows = self.query_ex(command, fetch=fetch)
        return pd.DataFrame(rows or [], columns=columns)

    def query_ex(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        fetch: str = "all",
        timeout: float | None = None,
    ):
        if params:
            raise MdlPolicyError("Parameterized raw SQL is not supported by MDL proxy.")
        principal = _require_current_principal()
        _assert_same_principal(self._principal, principal)
        plan = self._service.plan_authorized_sql(
            query,
            user_id=_principal_name(self._principal),
            data_source=infer_wren_data_source(self._raw_connector),
            connection_info=None,
            role=str(self._principal.get("role") or ""),
        )
        if not hasattr(self._raw_connector, "query_ex"):
            raise MdlPolicyError(
                "MDL protected connector requires raw connector.query_ex()."
            )
        kwargs: dict[str, Any] = {"fetch": fetch}
        if timeout is not None:
            kwargs["timeout"] = timeout
        else:
            kwargs["timeout"] = MDL_QUERY_TIMEOUT_SECONDS
        return self._raw_connector.query_ex(
            bounded_query_sql(plan.planned_sql),
            **kwargs,
        )

    def get_columns(self, table_name: str):
        schema = self._visible_schema()
        if table_name not in schema:
            raise MdlPolicyError(
                f"Table {table_name} is not visible through MDL policy."
            )
        return [
            {"name": column["name"], "type": column.get("type", "")}
            for column in schema[table_name]
        ]

    def get_fields(self, table_name, db_name=None):
        return [
            (column["name"], column.get("type", ""), None, "YES", "")
            for column in self._visible_schema()[table_name]
        ]

    def get_simple_fields(self, table_name):
        return [
            [("Field",)],
            *[(column["name"],) for column in self._visible_schema()[table_name]],
        ]

    @classmethod
    def is_normal_type(cls) -> bool:
        return True

    @classmethod
    def is_graph_type(cls) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        raise MdlPolicyError(
            f"MDL protected connector blocks raw connector method {name!r}."
        )

    def _visible_schema(self) -> dict[str, list[dict[str, str]]]:
        principal = _require_current_principal()
        _assert_same_principal(self._principal, principal)
        return self._service.get_schema(
            _principal_name(self._principal),
            role=str(self._principal.get("role") or ""),
        )


def _should_enable_mdl() -> bool:
    enabled = os.getenv(MDL_ENABLED_ENV)
    return enabled is not None and enabled.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _current_principal() -> dict[str, Any]:
    try:
        from .auth import current_principal  # type: ignore
    except Exception:
        return {}
    principal = current_principal.get()
    return principal if isinstance(principal, dict) else {}


def _require_current_principal() -> dict[str, Any]:
    principal = _current_principal()
    if not principal:
        raise MdlPolicyError(
            "MDL protected connector requires an authenticated principal."
        )
    return principal


def _principal_name(principal: dict[str, Any]) -> str | None:
    username = principal.get("username")
    return str(username) if username else None


def _assert_same_principal(
    captured: dict[str, Any],
    current: dict[str, Any],
) -> None:
    if _principal_name(captured) != _principal_name(current):
        raise MdlPolicyError(
            "MDL protected connector principal changed during request."
        )
