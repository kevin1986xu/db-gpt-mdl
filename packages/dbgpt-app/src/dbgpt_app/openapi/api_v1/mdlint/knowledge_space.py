"""Datasource-scoped bindings for regular DB-GPT knowledge spaces."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import tempfile
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .errors import MdlConfigError, MdlPolicyError
from .knowledge import manifest_fingerprint
from .manifest import resolve_manifest_path
from .models import MdlManifest, normalize_name
from .retrieval import tokenize
from .service import MdlQueryService

_SAVE_LOCK = threading.Lock()
_VALID_STATUSES = {"draft", "approved"}
_MAX_FALLBACK_CHUNKS = 5


def resolve_knowledge_space_path(
    manifest_path: str | Path | None,
    database: str,
) -> Path:
    digest = hashlib.sha256(database.encode("utf-8")).hexdigest()
    return resolve_manifest_path(manifest_path).with_name("mdl.knowledge_spaces") / (
        f"{digest}.json"
    )


class MdlKnowledgeSpaceService:
    """Manage reviewed knowledge-space bindings for one MDL datasource."""

    def __init__(
        self,
        manifest_path: str | Path | None = None,
        *,
        space_resolver: Any | None = None,
        fingerprint_provider: Any | None = None,
    ) -> None:
        self._manifest_path = manifest_path
        self._space_resolver = space_resolver or _resolve_space
        self._fingerprint_provider = fingerprint_provider or _space_fingerprint

    def load(self, database: str, *, include_stale: bool = False) -> dict[str, Any]:
        store = _read_store(
            resolve_knowledge_space_path(self._manifest_path, database),
            database,
        )
        return self._with_stale_flags(store, database) if include_stale else store

    def save(
        self,
        database: str,
        spaces: list[dict[str, Any]],
        revision: int,
    ) -> dict[str, Any]:
        path = resolve_knowledge_space_path(self._manifest_path, database)
        with _SAVE_LOCK:
            current = _read_store(path, database)
            if revision != current["revision"]:
                raise MdlConfigError(
                    "MDL knowledge-space revision conflict: "
                    f"expected {current['revision']}, got {revision}."
                )
            prior_by_id = {
                str(uuid.UUID(item["id"])): item for item in current["spaces"]
            }
            normalized = []
            seen_ids: set[str] = set()
            seen_spaces: set[str] = set()
            document = self._load_database_document(database)
            for raw in spaces:
                item = _normalize_binding(
                    raw,
                    prior=prior_by_id.get(_canonical_id(raw.get("id"))),
                )
                if item["id"] in seen_ids:
                    raise MdlConfigError(
                        f"Duplicate MDL knowledge-space binding id: {item['id']}."
                    )
                if item["space_name"] in seen_spaces:
                    raise MdlConfigError(
                        "Duplicate MDL knowledge-space binding for "
                        f"{item['space_name']}."
                    )
                self._validate_bindings(item, document)
                self._resolve_space(item["space_name"])
                seen_ids.add(item["id"])
                seen_spaces.add(item["space_name"])
                normalized.append(item)
            next_store = {
                "database": database,
                "revision": current["revision"] + 1,
                "spaces": normalized,
            }
            _atomic_write_json(path, next_store)
            return self._with_stale_flags(next_store, database)

    def approve(
        self,
        database: str,
        binding_id: str,
        revision: int,
        *,
        reviewed_by: str = "admin",
    ) -> dict[str, Any]:
        path = resolve_knowledge_space_path(self._manifest_path, database)
        binding_id = _canonical_id(binding_id) or binding_id
        with _SAVE_LOCK:
            store = _read_store(path, database)
            if revision != store["revision"]:
                raise MdlConfigError(
                    "MDL knowledge-space revision conflict: "
                    f"expected {store['revision']}, got {revision}."
                )
            document = self._load_database_document(database)
            fingerprint = manifest_fingerprint(document)
            entries = deepcopy(store["spaces"])
            for index, raw in enumerate(entries):
                if raw["id"] != binding_id:
                    continue
                item = _normalize_binding(raw)
                self._validate_bindings(item, document)
                space = self._resolve_space(item["space_name"])
                item["status"] = "approved"
                item["mdl_fingerprint"] = fingerprint
                item["space_fingerprint"] = self._space_fingerprint(space)
                item["validation"] = {
                    "mode": "space_bound",
                    "reviewed_by": reviewed_by,
                    "validated_at": datetime.now(timezone.utc).isoformat(),
                }
                entries[index] = item
                next_store = {
                    "database": database,
                    "revision": store["revision"] + 1,
                    "spaces": entries,
                }
                _atomic_write_json(path, next_store)
                return self._with_stale_flags(next_store, database)
            raise MdlConfigError(
                f"MDL knowledge-space binding not found: {binding_id}."
            )

    def authorized_spaces(
        self,
        database: str,
        principal: dict[str, Any] | None,
    ) -> list[str]:
        if not principal:
            return []
        document = self._load_database_document(database)
        fingerprint = manifest_fingerprint(document)
        result = []
        for item in self.load(database)["spaces"]:
            if item.get("status") != "approved":
                continue
            if item.get("mdl_fingerprint") != fingerprint:
                continue
            if self._is_space_stale(item):
                continue
            if not self._identity_authorized(item, principal):
                continue
            if not self._bindings_authorized(
                item,
                document,
                str(principal.get("username") or ""),
                str(principal.get("role") or ""),
            ):
                continue
            result.append(item["space_name"])
        return result

    def authorize_space(
        self,
        space_key: str,
        *,
        database: str,
        principal: dict[str, Any],
    ) -> str:
        try:
            canonical = _space_name(self._resolve_space(space_key))
        except Exception as exc:
            raise MdlPolicyError(
                "Knowledge space is not bound to this MDL datasource or user."
            ) from exc
        if canonical not in self.authorized_spaces(database, principal):
            raise MdlPolicyError(
                "Knowledge space is not bound to this MDL datasource or user."
            )
        return canonical

    def _with_stale_flags(
        self,
        store: dict[str, Any],
        database: str,
    ) -> dict[str, Any]:
        document = self._load_database_document(database)
        fingerprint = manifest_fingerprint(document)
        spaces = []
        for item in store["spaces"]:
            record = deepcopy(item)
            record["stale"] = record.get("status") == "approved" and (
                record.get("mdl_fingerprint") != fingerprint
                or self._is_space_stale(record)
            )
            spaces.append(record)
        return {**store, "spaces": spaces}

    def _load_database_document(self, database: str) -> dict[str, Any]:
        return MdlQueryService(manifest_path=self._manifest_path).load(database)

    def _resolve_space(self, space_key: str) -> Any:
        return self._space_resolver(space_key)

    def _space_fingerprint(self, space: Any) -> str:
        return self._fingerprint_provider(space)

    def _is_space_stale(self, item: dict[str, Any]) -> bool:
        if item.get("status") != "approved":
            return False
        try:
            space = self._resolve_space(item["space_name"])
            return self._space_fingerprint(space) != item.get("space_fingerprint")
        except Exception:
            return True

    def _validate_bindings(
        self,
        item: dict[str, Any],
        document: dict[str, Any],
    ) -> None:
        manifest = MdlManifest.from_dict(document)
        for raw_model, raw_columns in item["models"].items():
            model_name = normalize_name(raw_model)
            if model_name not in manifest.models:
                raise MdlConfigError(
                    f"Knowledge space references unknown MDL model: {raw_model}."
                )
            columns = {normalize_name(column) for column in raw_columns}
            unknown = columns - manifest.models[model_name].columns
            if unknown:
                raise MdlConfigError(
                    f"Knowledge space references unknown columns for {raw_model}: "
                    f"{', '.join(sorted(unknown))}."
                )

    def _bindings_authorized(
        self,
        item: dict[str, Any],
        document: dict[str, Any],
        user_id: str,
        role: str,
    ) -> bool:
        manifest = MdlManifest.from_dict(document)
        effective = manifest.effective_manifest(user_id, role=role)
        allowed = {
            normalize_name(model["name"]): {
                normalize_name(column["name"])
                for column in model.get("columns", [])
                if isinstance(column, dict) and column.get("name")
            }
            for model in effective.get("models", [])
        }
        for raw_model, raw_columns in item["models"].items():
            model_name = normalize_name(raw_model)
            columns = {normalize_name(column) for column in raw_columns}
            if model_name not in allowed or not columns <= allowed[model_name]:
                return False
        return True

    def _identity_authorized(
        self,
        item: dict[str, Any],
        principal: dict[str, Any],
    ) -> bool:
        username = str(principal.get("username") or "")
        role = str(principal.get("role") or "")
        users = set(item.get("users", []))
        roles = set(item.get("roles", []))
        return (not users or username in users) and (not roles or role in roles)


class MdlAuthorizedKnowledgeResource:
    """Retriever wrapper that re-checks MDL binding on every retrieval."""

    def __init__(
        self,
        *,
        delegate: Any,
        database: str,
        principal: dict[str, Any],
        space_name: str,
        service: MdlKnowledgeSpaceService | None = None,
    ) -> None:
        self._delegate = delegate
        self._database = database
        self._principal = principal
        self._space_name = space_name
        self._service = service or MdlKnowledgeSpaceService()
        self._retriever_name = getattr(delegate, "retriever_name", None) or space_name
        self._retriever_desc = getattr(delegate, "retriever_desc", None)

    @property
    def retriever_name(self) -> str:
        return self._retriever_name

    @property
    def retriever_desc(self) -> str | None:
        return self._retriever_desc

    async def retrieve(self, query: str):
        if not self._authorized():
            return []
        if self._delegate is None:
            chunks = _fallback_retrieve_chunks(self._space_name, query)
        else:
            try:
                chunks = await self._delegate.retrieve(query)
            except (ConnectionError, TimeoutError, OSError):
                # Only infrastructure outages permit a validated lexical
                # fallback. Invalid metadata and policy errors stay closed.
                chunks = _fallback_retrieve_chunks(self._space_name, query)
        if not self._authorized():
            return []
        return chunks

    def _authorized(self) -> bool:
        return self._space_name in self._service.authorized_spaces(
            self._database,
            self._principal,
        )


def authorize_mdl_knowledge_space(
    space_key: str,
    *,
    database: str,
    principal: dict[str, Any],
    manifest_path: str | None = None,
    space_dao: Any | None = None,
) -> str:
    resolver = (
        (lambda key: _resolve_space(key, space_dao=space_dao))
        if space_dao is not None
        else None
    )
    return MdlKnowledgeSpaceService(
        manifest_path=manifest_path,
        space_resolver=resolver,
    ).authorize_space(space_key, database=database, principal=principal)


def _read_store(path: Path, database: str) -> dict[str, Any]:
    if not path.exists():
        return {"database": database, "revision": 0, "spaces": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MdlConfigError(
            f"MDL knowledge-space store is not valid JSON: {exc}"
        ) from exc
    if not isinstance(raw, dict) or raw.get("database") != database:
        raise MdlConfigError("MDL knowledge-space store database mismatch.")
    spaces = raw.get("spaces")
    if not isinstance(spaces, list):
        raise MdlConfigError("MDL knowledge-space bindings must be a list.")
    return {
        "database": database,
        "revision": int(raw.get("revision", 0) or 0),
        "spaces": [_normalize_binding(item, preserve_approved=True) for item in spaces],
    }


def _normalize_binding(
    raw: dict[str, Any],
    *,
    prior: dict[str, Any] | None = None,
    preserve_approved: bool = False,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MdlConfigError("MDL knowledge-space binding must be a JSON object.")
    binding_id = str(raw.get("id") or uuid.uuid4())
    try:
        binding_id = str(uuid.UUID(binding_id))
    except ValueError as exc:
        raise MdlConfigError(
            "MDL knowledge-space binding id must be a UUID string."
        ) from exc
    title = _required_string(raw, "title")
    space_name = _required_string(raw, "space_name")
    models = _normalize_models(raw.get("models"))
    users = _normalize_string_list(raw.get("users", []), "users")
    roles = _normalize_string_list(raw.get("roles", []), "roles")
    status = str(raw.get("status", "draft"))
    if status not in _VALID_STATUSES:
        raise MdlConfigError(
            "MDL knowledge-space binding status must be draft or approved."
        )
    normalized = {
        "id": binding_id,
        "title": title,
        "space_name": space_name,
        "description": str(raw.get("description") or "").strip(),
        "models": models,
        "users": users,
        "roles": roles,
        "status": "draft",
    }
    audit_same = prior is not None and _audit_payload(prior) == _audit_payload(
        normalized
    )
    if preserve_approved and status == "approved" and raw.get("mdl_fingerprint"):
        normalized["status"] = "approved"
        normalized["mdl_fingerprint"] = str(raw["mdl_fingerprint"])
        normalized["space_fingerprint"] = str(raw.get("space_fingerprint") or "")
        if isinstance(raw.get("validation"), dict):
            normalized["validation"] = deepcopy(raw["validation"])
    elif (
        prior is not None
        and prior.get("status") == "approved"
        and audit_same
        and prior.get("mdl_fingerprint")
    ):
        normalized["status"] = "approved"
        normalized["mdl_fingerprint"] = prior["mdl_fingerprint"]
        normalized["space_fingerprint"] = prior.get("space_fingerprint", "")
        if isinstance(prior.get("validation"), dict):
            normalized["validation"] = deepcopy(prior["validation"])
    return normalized


def _normalize_models(raw_models: Any) -> dict[str, list[str]]:
    if not isinstance(raw_models, dict) or not raw_models:
        raise MdlConfigError(
            "MDL knowledge-space binding must bind at least one model field."
        )
    result: dict[str, list[str]] = {}
    for raw_model, raw_columns in raw_models.items():
        model = str(raw_model).strip()
        if not model:
            raise MdlConfigError("MDL knowledge-space model name cannot be empty.")
        if (
            not isinstance(raw_columns, list)
            or not raw_columns
            or not all(
                isinstance(column, str) and column.strip() for column in raw_columns
            )
        ):
            raise MdlConfigError(
                "MDL knowledge-space model bindings must be non-empty string lists."
            )
        result[model] = [column.strip() for column in raw_columns]
    return result


def _normalize_string_list(raw: Any, field: str) -> list[str]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise MdlConfigError(f"MDL knowledge-space {field} must be a string list.")
    return [item.strip() for item in raw if item.strip()]


def _required_string(raw: dict[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MdlConfigError(f"MDL knowledge-space binding requires {field}.")
    return value.strip()


def _canonical_id(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError):
        return str(value)


def _audit_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(item.get(key))
        for key in ("title", "space_name", "description", "models", "users", "roles")
    }


def _resolve_space(space_key: str, *, space_dao: Any | None = None) -> Any:
    if space_dao is None:
        from dbgpt_serve.rag.models.models import (
            KnowledgeSpaceDao,
            KnowledgeSpaceEntity,
        )

        space_dao = KnowledgeSpaceDao()
        entity_type = KnowledgeSpaceEntity
    else:
        entity_type = None
    if str(space_key).isdigit():
        spaces = space_dao.get_knowledge_space_by_ids([int(space_key)])
    else:
        if entity_type is None:
            spaces = space_dao.get_knowledge_space({"name": space_key})
        else:
            spaces = space_dao.get_knowledge_space(entity_type(name=space_key))
    if len(spaces) != 1:
        raise MdlPolicyError("Knowledge space was not found.")
    return spaces[0]


def _space_name(space: Any) -> str:
    value = getattr(space, "name", None)
    if not value:
        raise MdlPolicyError("Knowledge space has no name.")
    return str(value)


def _space_fingerprint(space: Any) -> str:
    payload = _space_payload(space)
    documents, chunks = _space_documents_and_chunks(getattr(space, "name", ""))
    for document in documents:
        payload["documents"].append(document)
    payload["chunks"] = chunks
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _space_payload(space: Any) -> dict[str, Any]:
    return {
        "id": getattr(space, "id", None),
        "name": getattr(space, "name", None),
        "desc": getattr(space, "desc", None),
        "index_methods": getattr(space, "index_methods", None),
        "context": getattr(space, "context", None),
        "gmt_modified": getattr(space, "gmt_modified", None),
        "documents": [],
        "chunks": [],
    }


def _space_documents_and_chunks(
    space_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not space_name:
        raise MdlPolicyError("Knowledge space has no name.")
    from dbgpt_serve.rag.models.chunk_db import DocumentChunkDao, DocumentChunkEntity
    from dbgpt_serve.rag.models.document_db import (
        KnowledgeDocumentDao,
        KnowledgeDocumentEntity,
    )

    doc_dao = KnowledgeDocumentDao()
    rows = []
    page = 1
    page_size = 500
    while True:
        page_rows = doc_dao.get_knowledge_documents(
            KnowledgeDocumentEntity(space=space_name),
            page=page,
            page_size=page_size,
        )
        rows.extend(page_rows)
        if len(page_rows) < page_size:
            break
        page += 1
    document_ids = [
        getattr(row, "id", None) for row in rows if getattr(row, "id", None) is not None
    ]
    chunk_rows = []
    if document_ids:
        chunk_dao = DocumentChunkDao()
        page = 1
        while True:
            page_rows = chunk_dao.get_document_chunks(
                DocumentChunkEntity(),
                page=page,
                page_size=page_size,
                document_ids=document_ids,
            )
            chunk_rows.extend(page_rows)
            if len(page_rows) < page_size:
                break
            page += 1
    documents = [
        {
            "id": getattr(row, "id", None),
            "doc_name": getattr(row, "doc_name", None),
            "doc_type": getattr(row, "doc_type", None),
            "doc_token_hash": _content_hash(getattr(row, "doc_token", None)),
            "chunk_size": getattr(row, "chunk_size", None),
            "content_hash": _content_hash(getattr(row, "content", None)),
            "result_hash": _content_hash(getattr(row, "result", None)),
            "questions_hash": _content_hash(getattr(row, "questions", None)),
            "status": getattr(row, "status", None),
            "last_sync": getattr(row, "last_sync", None),
            "summary": getattr(row, "summary", None),
            "gmt_modified": getattr(row, "gmt_modified", None),
            "vector_ids": getattr(row, "vector_ids", None),
        }
        for row in rows
    ]
    chunks = [
        {
            "id": getattr(row, "id", None),
            "document_id": getattr(row, "document_id", None),
            "doc_name": getattr(row, "doc_name", None),
            "content_hash": hashlib.sha256(
                str(getattr(row, "content", "") or "").encode("utf-8")
            ).hexdigest(),
            "questions_hash": hashlib.sha256(
                str(getattr(row, "questions", "") or "").encode("utf-8")
            ).hexdigest(),
            "meta_info": _parse_chunk_metadata(getattr(row, "meta_info", None)),
            "gmt_modified": getattr(row, "gmt_modified", None),
        }
        for row in chunk_rows
    ]
    return documents, chunks


def _content_hash(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _parse_chunk_metadata(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise MdlPolicyError("Knowledge chunk metadata must be a dictionary.")
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError, TypeError) as exc:
            raise MdlPolicyError("Knowledge chunk metadata is invalid.") from exc
    if not isinstance(parsed, dict):
        raise MdlPolicyError("Knowledge chunk metadata must be a dictionary.")
    return parsed


def _fallback_retrieve_chunks(space_name: str, query: str) -> list[Any]:
    query_tokens = tokenize(query)
    if not query_tokens:
        return []
    chunks = _space_chunk_contents(space_name)
    scored = []
    for index, chunk in enumerate(chunks):
        content = str(chunk.get("content") or "")
        overlap = query_tokens & tokenize(content)
        if not overlap:
            continue
        scored.append((len(overlap), index, chunk))
    scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return [
        SimpleNamespace(
            chunk_id=str(chunk.get("id") or f"{space_name}-{index + 1}"),
            chunk_name=chunk.get("doc_name") or space_name,
            content=chunk.get("content") or "",
            score=float(score),
            metadata={
                "doc_name": chunk.get("doc_name") or space_name,
                "document_id": chunk.get("document_id"),
                "source": chunk.get("doc_name") or space_name,
            },
        )
        for score, index, chunk in scored[:_MAX_FALLBACK_CHUNKS]
    ]


def _space_chunk_contents(space_name: str) -> list[dict[str, Any]]:
    from dbgpt_serve.rag.models.chunk_db import DocumentChunkDao, DocumentChunkEntity
    from dbgpt_serve.rag.models.document_db import (
        KnowledgeDocumentDao,
        KnowledgeDocumentEntity,
    )

    doc_rows = []
    doc_dao = KnowledgeDocumentDao()
    page = 1
    page_size = 500
    while True:
        page_rows = doc_dao.get_knowledge_documents(
            KnowledgeDocumentEntity(space=space_name),
            page=page,
            page_size=page_size,
        )
        doc_rows.extend(page_rows)
        if len(page_rows) < page_size:
            break
        page += 1
    document_ids = [
        getattr(row, "id", None)
        for row in doc_rows
        if getattr(row, "id", None) is not None
    ]
    if not document_ids:
        return []
    chunk_dao = DocumentChunkDao()
    result = []
    page = 1
    while True:
        page_rows = chunk_dao.get_document_chunks(
            DocumentChunkEntity(),
            page=page,
            page_size=page_size,
            document_ids=document_ids,
        )
        for row in page_rows:
            result.append(
                {
                    "id": getattr(row, "id", None),
                    "document_id": getattr(row, "document_id", None),
                    "doc_name": getattr(row, "doc_name", None),
                    "content": getattr(row, "content", None),
                    "metadata": _parse_chunk_metadata(getattr(row, "meta_info", None)),
                }
            )
        if len(page_rows) < page_size:
            break
        page += 1
    return result


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
