"""Executable analysis-rule checks for SQL result evidence."""

from decimal import Decimal

import pytest

from dbgpt_app.openapi.api_v1.mdlint.analysis_rules import (
    normalize_checks,
    validate_analysis_rules,
    values_equal,
)
from dbgpt_app.openapi.api_v1.mdlint.errors import MdlConfigError


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), Decimal("NaN"), Decimal("Infinity")]
)
@pytest.mark.parametrize("operator", ["eq", "in"])
def test_non_finite_scalar_assertions_are_rejected(value, operator):
    with pytest.raises(MdlConfigError, match="scalar"):
        normalize_checks(
            [
                {
                    "column": "n",
                    "operator": operator,
                    "value": [value] if operator == "in" else value,
                }
            ]
        )
    assert not values_equal(value, value)


def test_normalize_checks_accepts_declarative_value_checks():
    checks = normalize_checks(
        [
            {
                "column": "设备数",
                "operator": "between",
                "value": [1, 10],
                "message": "设备数应在范围内",
            },
            {"column": "SN", "operator": "not_null"},
        ]
    )

    assert checks[0]["value"] == [1, 10]
    assert checks[0]["severity"] == "error"
    assert checks[1]["message"] == "SN must satisfy not_null."


@pytest.mark.parametrize(
    "check",
    [
        {"column": "x", "operator": "__import__('os').system", "value": 1},
        {"column": "x", "operator": "gt", "value": "1"},
        {"column": "x", "operator": "between", "value": [0, "10"]},
        {"column": "x", "operator": "between", "value": [10, 0]},
        {"column": "x", "operator": "gt", "value": float("inf")},
        {"column": "x", "operator": "in", "value": [{"bad": True}]},
    ],
)
def test_normalize_checks_rejects_non_declarative_or_wrong_typed_checks(check):
    with pytest.raises(MdlConfigError):
        normalize_checks([check])


def test_validate_analysis_rules_reports_failed_and_missing_columns():
    report = validate_analysis_rules(
        [
            {
                "id": "rule-1",
                "title": "设备数口径",
                "rule": {
                    "metric": "设备数",
                    "checks": [
                        {
                            "column": "设备数",
                            "operator": "lt",
                            "value": 3,
                            "severity": "error",
                            "message": "设备数应小于三",
                        },
                        {
                            "column": "状态",
                            "operator": "not_null",
                            "severity": "warning",
                            "message": "状态需要返回",
                        },
                    ],
                },
            }
        ],
        ["设备数"],
        [[4]],
    )

    assert report["status"] == "failed"
    assert report["checks"][0]["status"] == "failed"
    assert report["checks"][0]["failed_rows"] == [1]
    assert report["checks"][1]["status"] == "not_applicable"


def test_decimal_values_are_compared_without_float_rounding():
    assert values_equal(Decimal("9007199254740993"), 9007199254740993)
    assert not values_equal(Decimal("9007199254740993"), 9007199254740992)
    report = validate_analysis_rules(
        [
            {
                "id": "rule-1",
                "title": "精确数值",
                "rule": {
                    "metric": "金额",
                    "checks": [
                        {
                            "column": "amount",
                            "operator": "gte",
                            "value": 9007199254740993,
                            "message": "金额达到阈值",
                        }
                    ],
                },
            }
        ],
        ["amount"],
        [[Decimal("9007199254740993")]],
    )

    assert report["status"] == "passed"


def test_duplicate_result_columns_and_unconfigured_rules_are_not_reported_passed():
    duplicate = validate_analysis_rules(
        [
            {
                "id": "rule-1",
                "title": "重复列",
                "rule": {
                    "metric": "设备数",
                    "checks": [{"column": "count", "operator": "not_null"}],
                },
            }
        ],
        ["count", "count"],
        [[1, None]],
    )
    assert duplicate["status"] == "not_applicable"
    assert "缺失或重复" in duplicate["checks"][0]["message"]

    partial = validate_analysis_rules(
        [
            {"id": "rule-1", "title": "无断言", "rule": {"metric": "设备数"}},
            {
                "id": "rule-2",
                "title": "有断言",
                "rule": {
                    "metric": "设备数",
                    "checks": [{"column": "count", "operator": "not_null"}],
                },
            },
        ],
        ["count"],
        [[1]],
    )
    assert partial["status"] == "not_applicable"
