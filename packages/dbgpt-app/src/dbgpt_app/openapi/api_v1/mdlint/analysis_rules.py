"""Executable checks for MDL analysis-rule knowledge."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from numbers import Real
from typing import Any

from .errors import MdlConfigError

ALLOWED_OPERATORS = {
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "between",
    "in",
    "not_null",
    "is_null",
}
ALLOWED_SEVERITIES = {"error", "warning"}
NULL_OPERATORS = {"not_null", "is_null"}
NUMERIC_OPERATORS = {"gt", "gte", "lt", "lte"}


def normalize_checks(raw_checks: Any) -> list[dict[str, Any]]:
    """Normalize declarative row checks without accepting executable code."""
    if raw_checks in (None, ""):
        return []
    if not isinstance(raw_checks, list):
        raise MdlConfigError("Analysis rule checks must be a list.")
    checks: list[dict[str, Any]] = []
    for raw_check in raw_checks:
        if not isinstance(raw_check, dict):
            raise MdlConfigError("Each analysis rule check must be an object.")
        column = str(raw_check.get("column") or "").strip()
        if not column:
            raise MdlConfigError("Analysis rule checks require column.")
        operator = str(raw_check.get("operator") or "").strip()
        if operator not in ALLOWED_OPERATORS:
            raise MdlConfigError(
                "Analysis rule check operator must be one of: "
                + ", ".join(sorted(ALLOWED_OPERATORS))
            )
        severity = str(raw_check.get("severity") or "error").strip()
        if severity not in ALLOWED_SEVERITIES:
            raise MdlConfigError(
                "Analysis rule check severity must be error or warning."
            )
        message = str(raw_check.get("message") or "").strip()
        check: dict[str, Any] = {
            "column": column,
            "operator": operator,
            "severity": severity,
            "message": message or f"{column} must satisfy {operator}.",
        }
        if operator in NULL_OPERATORS:
            checks.append(check)
            continue
        if "value" not in raw_check:
            raise MdlConfigError("Analysis rule check requires value.")
        value = raw_check.get("value")
        if operator == "between":
            if (
                not isinstance(value, list)
                or len(value) != 2
                or not all(_is_number(item) for item in value)
            ):
                raise MdlConfigError(
                    "Analysis rule between checks require two numbers."
                )
            lower = _to_decimal(value[0])
            upper = _to_decimal(value[1])
            if lower is None or upper is None or lower > upper:
                raise MdlConfigError(
                    "Analysis rule between checks require an ascending range."
                )
            check["value"] = list(value)
        elif operator == "in":
            if not isinstance(value, list) or not value:
                raise MdlConfigError(
                    "Analysis rule in checks require a non-empty list."
                )
            if not all(_is_scalar(item) for item in value):
                raise MdlConfigError(
                    "Analysis rule in checks require scalar expected values."
                )
            check["value"] = list(value)
        elif operator in NUMERIC_OPERATORS:
            if not _is_number(value):
                raise MdlConfigError("Analysis rule numeric checks require a number.")
            check["value"] = value
        else:
            if not _is_scalar(value):
                raise MdlConfigError("Analysis rule equality checks require a scalar.")
            check["value"] = value
        checks.append(check)
    return checks


def normalize_report_requirements(raw_requirements: Any) -> list[str]:
    if raw_requirements in (None, ""):
        return []
    if not isinstance(raw_requirements, list) or not all(
        isinstance(item, str) and item.strip() for item in raw_requirements
    ):
        raise MdlConfigError("Analysis rule report_requirements must be a string list.")
    return [item.strip() for item in raw_requirements]


def validate_analysis_rules(
    rules: list[dict[str, Any]],
    columns: list[str],
    rows: list[Any],
) -> dict[str, Any]:
    """Validate authorized matching rules against the full SQL result set."""
    if not rules:
        return {"status": "not_configured", "checks": [], "rules": []}
    rule_reports = []
    flat_checks = []
    for rule_entry in rules:
        rule = rule_entry.get("rule") or {}
        checks = rule.get("checks") or []
        if not checks:
            report = {
                "id": rule_entry.get("id"),
                "title": rule_entry.get("title"),
                "status": "not_configured",
                "checks": [],
            }
            rule_reports.append(report)
            continue
        check_reports = [
            _validate_check(check, columns, rows)
            for check in checks
            if isinstance(check, dict)
        ]
        status = _aggregate_status(check_reports)
        report = {
            "id": rule_entry.get("id"),
            "title": rule_entry.get("title"),
            "metric": rule.get("metric"),
            "status": status,
            "checks": check_reports,
            "report_requirements": list(rule.get("report_requirements") or []),
        }
        rule_reports.append(report)
        flat_checks.extend(check_reports)
    return {
        "status": _aggregate_status(rule_reports),
        "checks": flat_checks,
        "rules": rule_reports,
    }


def _validate_check(
    check: dict[str, Any],
    columns: list[str],
    rows: list[Any],
) -> dict[str, Any]:
    column = str(check.get("column") or "")
    message = str(check.get("message") or column)
    if columns.count(column) != 1:
        return {
            "status": "not_applicable",
            "column": column,
            "operator": check.get("operator"),
            "severity": check.get("severity", "error"),
            "message": f"{message}（结果列 {column} 缺失或重复）",
        }
    if not rows:
        return {
            "status": "not_applicable",
            "column": column,
            "operator": check.get("operator"),
            "severity": check.get("severity", "error"),
            "message": f"{message}（查询结果为空）",
        }
    index = columns.index(column)
    failed_rows = []
    for row_index, row in enumerate(rows, start=1):
        values = list(row) if isinstance(row, (list, tuple)) else [row]
        value = values[index] if index < len(values) else None
        if not _check_value(value, check):
            failed_rows.append(row_index)
    if failed_rows:
        return {
            "status": "failed",
            "column": column,
            "operator": check.get("operator"),
            "severity": check.get("severity", "error"),
            "message": message,
            "failed_rows": failed_rows[:20],
        }
    return {
        "status": "passed",
        "column": column,
        "operator": check.get("operator"),
        "severity": check.get("severity", "error"),
        "message": message,
    }


def _check_value(value: Any, check: dict[str, Any]) -> bool:
    operator = check.get("operator")
    expected = check.get("value")
    if operator == "not_null":
        return value is not None
    if operator == "is_null":
        return value is None
    if operator == "eq":
        return values_equal(value, expected)
    if operator == "ne":
        return not values_equal(value, expected)
    if operator == "in":
        return any(values_equal(value, item) for item in expected)
    if operator == "between":
        actual = _to_decimal(value)
        lower = _to_decimal(expected[0])
        upper = _to_decimal(expected[1])
        return (
            actual is not None
            and lower is not None
            and upper is not None
            and lower <= actual <= upper
        )
    if operator in NUMERIC_OPERATORS:
        actual = _to_decimal(value)
        wanted = _to_decimal(expected)
        if actual is None or wanted is None:
            return False
        if operator == "gt":
            return actual > wanted
        if operator == "gte":
            return actual >= wanted
        if operator == "lt":
            return actual < wanted
        if operator == "lte":
            return actual <= wanted
    return False


def _aggregate_status(items: list[dict[str, Any]]) -> str:
    statuses = [item.get("status") for item in items]
    if any(status == "failed" for status in statuses):
        return "failed"
    if any(status in {"not_applicable", "not_configured"} for status in statuses):
        return "not_applicable"
    if statuses and all(status == "passed" for status in statuses):
        return "passed"
    return "not_configured"


def _is_scalar(value: Any) -> bool:
    return is_finite_scalar(value)


def is_finite_scalar(value: Any) -> bool:
    return (
        value is None
        or isinstance(value, (str, bool))
        or (isinstance(value, (int, float, Decimal)) and _is_number(value))
    )


def _is_number(value: Any) -> bool:
    return _to_decimal(value) is not None


def values_equal(actual: Any, expected: Any) -> bool:
    if not is_finite_scalar(actual) or not is_finite_scalar(expected):
        return False
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if _is_number(actual) and _is_number(expected):
        return _to_decimal(actual) == _to_decimal(expected)
    return type(actual) is type(expected) and actual == expected


def _to_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None
