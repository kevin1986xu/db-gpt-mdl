"""Deterministic retrieval scoring for datasource-scoped MDL knowledge."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Any, Iterable

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_CHINESE_CHUNK = re.compile(r"[\u4e00-\u9fff]+")
_LOW_SIGNAL_TOKENS = {
    "查询",
    "统计",
    "数量",
    "多少",
    "量是",
    "是多",
    "几个",
    "多少个",
    "一下",
}


def question_key(value: str) -> str:
    value = re.sub(r"^\[Database:[^\]]+\]\s*", "", value, flags=re.IGNORECASE)
    return re.sub(r"[\s\W_]+", "", value.lower(), flags=re.UNICODE)


def is_exact_match(entry: dict[str, Any], question: str) -> bool:
    query_key = question_key(question)
    if not query_key:
        return False
    candidates = [entry.get("question", ""), *entry.get("aliases", [])]
    return any(question_key(str(candidate)) == query_key for candidate in candidates)


def tokenize(text: str) -> set[str]:
    lowered = text.lower()
    tokens = set(_IDENTIFIER.findall(lowered))
    for chunk in _CHINESE_CHUNK.findall(lowered):
        tokens.add(chunk)
        tokens.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return {token for token in tokens if token}


def score_entry(
    entry: dict[str, Any],
    question: str,
    *,
    corpus: Iterable[dict[str, Any]] | None = None,
    expanded_question: str | None = None,
) -> float:
    expanded = expanded_question or question
    query_tokens = tokenize(expanded)
    if not query_tokens:
        return 0
    entry_tokens = tokenize(_entry_text(entry))
    overlap = query_tokens & entry_tokens
    high_signal_overlap = overlap - _LOW_SIGNAL_TOKENS
    if not high_signal_overlap:
        return 0

    document_frequency = _document_frequency(corpus or [entry])
    corpus_size = max(1, len(document_frequency.get("__documents__", [])))
    score = 0.0
    for token in high_signal_overlap:
        score += 1.0 + math.log((corpus_size + 1) / (document_frequency[token] + 1))
    score += min(len(overlap & _LOW_SIGNAL_TOKENS), 2) * 0.15

    lowered = expanded.lower()
    for alias in entry.get("aliases", []):
        if alias and str(alias).lower() in lowered:
            score += 3.0
    rule = entry.get("rule", {})
    metric = rule.get("metric") if isinstance(rule, dict) else None
    if metric and str(metric) in expanded:
        score += 2.5
    if entry.get("kind") == "qa_sql" and _asks_for_metric(expanded):
        score += 6.0
    if is_exact_match(entry, question):
        score += 12.0
    return round(score, 6)


def score_entries(entries: list[dict[str, Any]], question: str) -> list[dict[str, Any]]:
    expanded_question = _expand_question_with_glossary(entries, question)
    scored = []
    for entry in entries:
        score = score_entry(
            entry,
            question,
            corpus=entries,
            expanded_question=expanded_question,
        )
        if score <= 0:
            continue
        scored.append(
            {
                **entry,
                "score": score,
                "exact_match": is_exact_match(entry, question),
            }
        )
    return scored


def rank_entries(
    entries: list[dict[str, Any]],
    question: str,
    *,
    top_k: int = 4,
) -> list[dict[str, Any]]:
    scored = score_entries(entries, question)
    limit = max(1, min(int(top_k or 4), 20))
    return sorted(
        scored,
        key=lambda item: (bool(item.get("exact_match")), item["score"]),
        reverse=True,
    )[:limit]


def _entry_text(entry: dict[str, Any]) -> str:
    return " ".join(
        [
            str(entry.get("title", "")),
            str(entry.get("question", "")),
            str(entry.get("content", "")),
            str(entry.get("sql", "")),
            " ".join(str(alias) for alias in entry.get("aliases", [])),
            " ".join(str(model) for model in entry.get("models", {}).keys()),
            " ".join(
                str(column)
                for columns in entry.get("models", {}).values()
                for column in columns
            ),
            json.dumps(entry.get("terms", []), ensure_ascii=False),
            json.dumps(entry.get("rule", {}), ensure_ascii=False),
        ]
    )


def _document_frequency(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    documents = list(entries)
    frequencies: Counter[str] = Counter()
    for entry in documents:
        frequencies.update(tokenize(_entry_text(entry)))
    result: dict[str, Any] = dict(frequencies)
    result["__documents__"] = documents
    return result


def _expand_question_with_glossary(entries: list[dict[str, Any]], question: str) -> str:
    additions = []
    for entry in entries:
        if entry.get("kind") != "glossary":
            continue
        for term in entry.get("terms", []):
            candidates = [
                str(term.get("term") or ""),
                *map(str, term.get("aliases", [])),
            ]
            if any(candidate and candidate in question for candidate in candidates):
                additions.extend(candidate for candidate in candidates if candidate)
    if not additions:
        return question
    return " ".join([question, *additions])


def _asks_for_metric(question: str) -> bool:
    return any(
        marker in question
        for marker in (
            "数量",
            "多少",
            "几个",
            "几架",
            "总数",
            "合计",
            "计数",
            "count",
            "sum",
            "avg",
        )
    )
