"""Typed MDL manifest and per-user policy projection."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .errors import MdlConfigError


def normalize_name(value: str) -> str:
    """Normalize SQL object names for policy matching."""
    return value.strip().strip('"`[]').lower()


@dataclass(frozen=True)
class MdlModel:
    name: str
    columns: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class FilterRule:
    column: str
    op: str
    value: str | int | float | bool


@dataclass(frozen=True)
class ModelAccessPolicy:
    columns: frozenset[str]
    filters: tuple[FilterRule, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class UserAccessPolicy:
    username: str
    models: dict[str, ModelAccessPolicy] = field(default_factory=dict)


@dataclass(frozen=True)
class MdlManifest:
    raw: dict[str, Any]
    models: dict[str, MdlModel]
    user_policies: dict[str, UserAccessPolicy] = field(default_factory=dict)
    raw_policies: list[dict[str, Any]] = field(default_factory=list)
    revision: int = 1
    database: str = "mdl_demo"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MdlManifest":
        if not isinstance(raw, dict):
            raise MdlConfigError("MDL manifest must be a JSON object.")

        raw_manifest, policies, revision, database = _unwrap_document(raw)
        raw_manifest = normalize_manifest(raw_manifest)
        raw_models = raw_manifest.get("models")
        if not isinstance(raw_models, list) or not raw_models:
            raise MdlConfigError("MDL manifest must contain a non-empty models list.")

        models: dict[str, MdlModel] = {}
        for raw_model in raw_models:
            if not isinstance(raw_model, dict) or not raw_model.get("name"):
                raise MdlConfigError("Every MDL model must be an object with a name.")
            model_name = normalize_name(str(raw_model["name"]))
            models[model_name] = MdlModel(
                name=str(raw_model["name"]),
                columns=_extract_column_names(raw_model),
            )

        raw_policy_list = _normalize_policies(policies)
        user_policies: dict[str, UserAccessPolicy] = {}
        seen_users: set[str] = set()
        for raw_policy in raw_policy_list:
            parsed = _parse_user_policy(raw_policy, models)
            if parsed.username in seen_users:
                raise MdlConfigError(
                    f"Duplicate MDL policy for user {parsed.username}."
                )
            seen_users.add(parsed.username)
            if raw_policy.get("enabled", True):
                user_policies[parsed.username] = parsed

        return cls(
            raw=raw_manifest,
            models=models,
            user_policies=user_policies,
            raw_policies=raw_policy_list,
            revision=revision,
            database=database,
        )

    def policy_for(self, user_id: str | None) -> UserAccessPolicy | None:
        if user_id is None:
            return None
        return self.user_policies.get(user_id)

    def schema_for(
        self,
        user_id: str | None,
        role: str | None = None,
    ) -> dict[str, list[dict[str, str]]]:
        manifest = self.effective_manifest(user_id, role=role)
        return {
            model["name"]: [
                {"name": column["name"], "type": column.get("type", "")}
                for column in model.get("columns", [])
                if not column.get("relationship")
            ]
            for model in manifest.get("models", [])
        }

    def to_wren_manifest(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.raw.items()
            if key not in {"policies", "relations", "revision", "status", "database"}
        }

    def to_document(self) -> dict[str, Any]:
        return {
            "manifest": self.to_wren_manifest(),
            "policies": self.raw_policies,
            "revision": self.revision,
            "database": self.database,
        }

    def effective_manifest(
        self,
        user_id: str | None,
        role: str | None = None,
    ) -> dict[str, Any]:
        if role == "admin":
            return deepcopy(self.to_wren_manifest())
        policy = self.policy_for(user_id)
        if policy is None:
            result = deepcopy(self.to_wren_manifest())
            result["models"] = []
            result["relationships"] = []
            return result
        return _manifest_for_policy(self.to_wren_manifest(), policy)


def normalize_manifest(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize the UI-friendly document into Wren's manifest JSON shape."""
    normalized = deepcopy(raw)
    default_schema = normalized.get("schema") or "main"
    normalized["models"] = [
        _normalize_model(model, default_schema=default_schema)
        for model in normalized.get("models", [])
        if isinstance(model, dict) and model.get("enabled", True)
    ]
    relationships = normalized.get("relationships", normalized.get("relations", []))
    normalized["relationships"] = [
        _normalize_relationship(rel)
        for rel in relationships
        if isinstance(rel, dict) and rel.get("enabled", True)
    ]
    return normalized


def _normalize_model(
    model: dict[str, Any],
    *,
    default_schema: str = "main",
) -> dict[str, Any]:
    normalized = deepcopy(model)
    table = normalized.pop("table", None)
    schema = normalized.pop("schema", None)
    if table and "tableReference" not in normalized:
        normalized["tableReference"] = {
            "schema": schema or default_schema,
            "table": table,
        }
    primary_key = normalized.pop("primary_key", None)
    if primary_key and "primaryKey" not in normalized:
        normalized["primaryKey"] = primary_key
    ref_sql = normalized.pop("ref_sql", None)
    if ref_sql and "refSql" not in normalized:
        normalized["refSql"] = ref_sql
    for key in ("columns", "dimensions", "measures"):
        if isinstance(normalized.get(key), list):
            normalized[key] = [
                _normalize_column(column)
                for column in normalized[key]
                if _column_enabled(column)
            ]
    return normalized


def _normalize_column(column: Any) -> Any:
    if not isinstance(column, dict):
        return column
    normalized = deepcopy(column)
    expression = normalized.pop("sql", None)
    if expression and "expression" not in normalized:
        normalized["expression"] = expression
    return normalized


def _column_enabled(column: Any) -> bool:
    if not isinstance(column, dict):
        return True
    return column.get("enabled", column.get("visible", True))


def _normalize_relationship(relationship: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(relationship)
    join_type = normalized.pop("join_type", None) or normalized.pop("type", None)
    if join_type and "joinType" not in normalized:
        normalized["joinType"] = join_type
    return normalized


def _unwrap_document(raw: dict[str, Any]) -> tuple[dict[str, Any], Any, int, str]:
    if "manifest" not in raw:
        return (
            raw,
            raw.get("policies", []),
            _parse_revision(raw.get("revision", 1)),
            str(raw.get("database", "mdl_demo")),
        )
    manifest = raw.get("manifest")
    if not isinstance(manifest, dict):
        raise MdlConfigError("MDL document manifest must be a JSON object.")
    return (
        manifest,
        raw.get("policies", manifest.get("policies", [])),
        _parse_revision(raw.get("revision", 1)),
        str(raw.get("database", "mdl_demo")),
    )


def _parse_revision(raw_revision: Any) -> int:
    if isinstance(raw_revision, bool):
        raise MdlConfigError("MDL revision must be an integer.")
    try:
        revision = int(raw_revision)
    except (TypeError, ValueError) as exc:
        raise MdlConfigError("MDL revision must be an integer.") from exc
    if revision < 1:
        raise MdlConfigError("MDL revision must be >= 1.")
    return revision


def _extract_column_names(raw_model: dict[str, Any]) -> frozenset[str]:
    columns: set[str] = set()
    for key in ("columns", "dimensions", "measures"):
        raw_columns = raw_model.get(key, [])
        if not isinstance(raw_columns, list):
            raise MdlConfigError(f"MDL model {raw_model['name']} has invalid {key}.")
        for raw_column in raw_columns:
            if isinstance(raw_column, dict) and raw_column.get("name"):
                columns.add(normalize_name(str(raw_column["name"])))
            elif isinstance(raw_column, str):
                columns.add(normalize_name(raw_column))
            else:
                raise MdlConfigError(
                    f"MDL model {raw_model['name']} has an invalid column entry."
                )
    return frozenset(columns)


def _normalize_policies(raw_policies: Any) -> list[dict[str, Any]]:
    if raw_policies is None:
        return []
    if isinstance(raw_policies, list):
        if not all(isinstance(policy, dict) for policy in raw_policies):
            raise MdlConfigError("MDL policies entries must be JSON objects.")
        return raw_policies
    raise MdlConfigError("MDL policies must be a list of user policy objects.")


def _parse_user_policy(
    raw_policy: Any,
    models: dict[str, MdlModel],
) -> UserAccessPolicy:
    if not isinstance(raw_policy, dict):
        raise MdlConfigError("Each MDL user policy must be a JSON object.")
    username = raw_policy.get("username")
    if not isinstance(username, str) or not username:
        raise MdlConfigError("Each MDL policy must include username.")
    raw_models = raw_policy.get("models", {})
    if not isinstance(raw_models, dict):
        raise MdlConfigError("Policy models must be a JSON object.")

    parsed_models: dict[str, ModelAccessPolicy] = {}
    for raw_name, raw_model_policy in raw_models.items():
        model_name = normalize_name(str(raw_name))
        if model_name not in models:
            raise MdlConfigError(f"Policy references unknown MDL model: {raw_name}.")
        if not isinstance(raw_model_policy, dict):
            raise MdlConfigError(f"Policy for model {raw_name} must be an object.")
        parsed_models[model_name] = ModelAccessPolicy(
            columns=_parse_columns(raw_name, raw_model_policy, models[model_name]),
            filters=_parse_filters(
                raw_model_policy.get("filters", []),
                models[model_name],
            ),
        )
    return UserAccessPolicy(username=username, models=parsed_models)


def _parse_columns(
    raw_name: Any,
    raw_model_policy: dict[str, Any],
    model: MdlModel,
) -> frozenset[str]:
    raw_columns = raw_model_policy.get("columns")
    if not isinstance(raw_columns, list) or not raw_columns or not all(
        isinstance(item, str) for item in raw_columns
    ):
        raise MdlConfigError(
            f"Policy columns for model {raw_name} must be a non-empty string list."
        )
    allowed_columns = frozenset(normalize_name(item) for item in raw_columns)
    unknown = allowed_columns - model.columns
    if unknown:
        raise MdlConfigError(
            f"Policy for model {raw_name} references unknown columns: "
            f"{', '.join(sorted(unknown))}."
        )
    return allowed_columns


def _parse_filters(raw_filters: Any, model: MdlModel) -> tuple[FilterRule, ...]:
    if not isinstance(raw_filters, list):
        raise MdlConfigError("Policy filters must be a list.")
    filters: list[FilterRule] = []
    for raw_filter in raw_filters:
        if not isinstance(raw_filter, dict):
            raise MdlConfigError("Policy filter entries must be JSON objects.")
        column = raw_filter.get("column")
        op = raw_filter.get("op")
        value = raw_filter.get("value")
        if not isinstance(column, str) or normalize_name(column) not in model.columns:
            raise MdlConfigError(f"Filter column is invalid for model {model.name}.")
        if op != "eq":
            raise MdlConfigError("Row filters currently support only op='eq'.")
        if not isinstance(value, (str, int, float, bool)):
            raise MdlConfigError("Row filter value must be scalar.")
        filters.append(FilterRule(column=column, op=op, value=value))
    return tuple(filters)


def _manifest_for_policy(
    manifest: dict[str, Any],
    policy: UserAccessPolicy,
) -> dict[str, Any]:
    import sqlglot
    from sqlglot import exp

    result = deepcopy(manifest)
    grants = policy.models
    result["models"] = [
        model
        for model in result.get("models", [])
        if normalize_name(model["name"]) in grants
    ]
    allowed_names = {normalize_name(model["name"]) for model in result["models"]}
    result["relationships"] = [
        relation
        for relation in result.get("relationships", [])
        if {normalize_name(name) for name in relation.get("models", [])}
        <= allowed_names
    ]
    result["relationships"] = [
        relation
        for relation in result["relationships"]
        if all(
            normalize_name(column.name)
            in grants[normalize_name(column.table)].columns
            for column in sqlglot.parse_one(
                relation.get("condition", ""),
                read="duckdb",
            ).find_all(exp.Column)
        )
    ]
    relation_names = {relation["name"] for relation in result["relationships"]}
    for model in result["models"]:
        grant = grants[normalize_name(model["name"])]
        original = {column["name"]: column for column in model.get("columns", [])}
        conditions = []
        for rule in grant.filters:
            column = original.get(rule.column)
            if not column or column.get("isCalculated"):
                raise MdlConfigError(
                    "Row filter depends on a changed or calculated field."
                )
            source = column.get("expression") or _quoted(column["name"])
            conditions.append(
                exp.EQ(
                    this=sqlglot.parse_one(source, read="duckdb"),
                    expression=exp.convert(rule.value),
                )
            )
        _apply_model_filter(model, conditions)
        model["columns"] = [
            column
            for column in model.get("columns", [])
            if (
                column.get("relationship") in relation_names
                if column.get("relationship")
                else normalize_name(column["name"]) in grant.columns
            )
        ]
        if model.get("primaryKey") not in {
            column["name"] for column in model["columns"]
        }:
            model.pop("primaryKey", None)
    _drop_unavailable_computed_fields(result)
    return result


def _apply_model_filter(model: dict[str, Any], conditions: list[Any]) -> None:
    import sqlglot
    from sqlglot import exp

    table_ref = model.pop("tableReference", None)
    if table_ref:
        table = table_ref["table"]
        schema = table_ref.get("schema", "main")
        base = exp.select("*").from_(f'{_quoted(schema)}.{_quoted(table)}')
    else:
        ref_sql = model.get("refSql")
        if not ref_sql:
            return
        base = sqlglot.parse_one(
            f"SELECT * FROM ({ref_sql}) AS mdl_source",
            read="duckdb",
        )
    if conditions:
        base = base.where(exp.and_(*conditions))
    model["refSql"] = base.sql(dialect="duckdb")


def _drop_unavailable_computed_fields(manifest: dict[str, Any]) -> None:
    import sqlglot
    from sqlglot import exp

    changed = True
    while changed:
        changed = False
        model_map = {
            model["name"]: {
                column["name"]: column for column in model.get("columns", [])
            }
            for model in manifest.get("models", [])
        }
        for model in manifest.get("models", []):
            kept = []
            for column in model.get("columns", []):
                permitted = True
                if column.get("isCalculated") and column.get("expression"):
                    for dep in sqlglot.parse_one(
                        column["expression"],
                        read="duckdb",
                    ).find_all(exp.Column):
                        current = model["name"]
                        for part in dep.parts[:-1]:
                            handle = model_map.get(current, {}).get(part.name, {})
                            if not handle.get("relationship"):
                                permitted = False
                                break
                            current = handle["type"]
                        if dep.name not in model_map.get(current, {}):
                            permitted = False
                if permitted:
                    kept.append(column)
                else:
                    changed = True
            model["columns"] = kept


def _quoted(value: str) -> str:
    return f'"{value}"'
