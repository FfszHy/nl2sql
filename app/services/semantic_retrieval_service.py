import hashlib
import json
import math
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.core.errors import AppError
from app.services.semantic_config_service import load_semantic_config_maps
from app.services.sqlite_store import get_connection, init_sqlite_store

def _schema_signature(schema: list[dict[str, Any]]) -> str:
    normalized = json.dumps(schema, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _fallback_table_comment(table_name: str, raw_comment: str) -> str:
    value = (raw_comment or "").strip()
    return value or table_name


def _fallback_field_comment(column_name: str, raw_comment: str) -> str:
    value = (raw_comment or "").strip()
    return value or column_name


def _fallback_field_type(raw_type: str) -> str:
    value = (raw_type or "").strip()
    return value or "unknown"


def _fallback_aliases(column_name: str, field_comment: str, aliases: list[str]) -> list[str]:
    cleaned = [str(item).strip() for item in aliases if str(item).strip()]
    if cleaned:
        return list(dict.fromkeys(cleaned))
    generated = [field_comment.strip(), column_name.strip()]
    return list(dict.fromkeys([item for item in generated if item]))


def build_semantic_schema_documents(
    schema: list[dict[str, Any]],
    field_config_map: dict[tuple[str, str], dict[str, Any]],
    table_config_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for table in schema:
        table_name = str(table["table_name"])
        raw_table_comment = str(table.get("table_comment") or "")
        table_cfg = table_config_map.get(table_name)
        table_comment = (
            str(table_cfg.get("table_comment") or "").strip()
            if table_cfg is not None
            else ""
        )
        table_comment = table_comment or _fallback_table_comment(table_name, raw_table_comment)
        columns = table["columns"]
        aliases: set[str] = set()
        column_lines = []
        for col in columns:
            column_name = str(col["name"])
            configured = field_config_map.get((table_name, column_name))
            raw_comment = str(col.get("comment") or "")
            raw_type = str(col.get("type") or "")
            field_comment = (
                str(configured.get("field_comment") or "").strip()
                if configured is not None
                else ""
            )
            field_comment = field_comment or _fallback_field_comment(column_name, raw_comment)
            field_type = (
                str(configured.get("field_type") or "").strip()
                if configured is not None
                else ""
            )
            field_type = field_type or _fallback_field_type(raw_type)
            configured_aliases = configured.get("field_aliases", []) if configured else []
            field_aliases = _fallback_aliases(column_name, field_comment, configured_aliases)
            aliases.update([str(alias) for alias in field_aliases if str(alias).strip()])
            column_lines.append(
                f"{column_name}|type={field_type}|nullable={col['nullable']}|comment={field_comment}"
            )
        sorted_aliases = sorted([item for item in aliases if item])
        alias_text = "，".join(sorted_aliases) if sorted_aliases else "无"
        semantic_text = "\n".join(
            [
                f"表名: {table_name}",
                f"表注释: {table_comment}",
                f"业务别名: {alias_text}",
                "字段列表:",
                *column_lines,
            ]
        )
        documents.append(
            {
                "table_name": table_name,
                "table_comment": table_comment,
                "aliases": sorted_aliases,
                "semantic_text": semantic_text,
            }
        )
    return documents


def _embedding_headers() -> dict[str, str]:
    if not settings.embedding_api_key:
        raise AppError(
            code=1020,
            message="缺少 embedding API Key 配置",
            error_type="llm_error",
            status_code=500,
        )
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.embedding_api_key}",
    }


def _extract_embedding_result(result: dict[str, Any], expected_count: int) -> list[list[float]]:
    status_code = result.get("status_code")
    code = result.get("code")
    message = result.get("message")
    if status_code and status_code != 200:
        raise AppError(
            code=1021,
            message=f"Embedding 业务错误: code={code}, message={message}",
            error_type="llm_error",
            status_code=502,
        )
    output = result.get("output")
    if not isinstance(output, dict):
        raise AppError(
            code=1022,
            message="Embedding 返回格式错误: output 缺失",
            error_type="llm_error",
            status_code=502,
        )
    items = output.get("embeddings")
    if not isinstance(items, list):
        raise AppError(
            code=1022,
            message="Embedding 返回格式错误: embeddings 缺失",
            error_type="llm_error",
            status_code=502,
        )
    vectors: list[list[float]] = [None] * expected_count  # type: ignore[list-item]
    for item in items:
        if not isinstance(item, dict):
            continue
        text_index = item.get("text_index")
        embedding = item.get("embedding")
        if not isinstance(text_index, int):
            continue
        if text_index < 0 or text_index >= expected_count:
            continue
        if not isinstance(embedding, list):
            continue
        vectors[text_index] = [float(v) for v in embedding]
    if any(vector is None for vector in vectors):
        raise AppError(
            code=1022,
            message="Embedding 返回数量与请求不一致",
            error_type="llm_error",
            status_code=502,
        )
    return vectors  # type: ignore[return-value]


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    payload = {
        "model": settings.embedding_model,
        "input": {"texts": texts},
        "parameters": {
            "dimension": settings.embedding_dimension,
            "output_type": "dense",
        },
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url=settings.embedding_api_url,
        data=body,
        headers=_embedding_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.embedding_timeout_seconds) as response:
            response_text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise AppError(
            code=1021,
            message=f"Embedding 调用失败: {detail or exc.reason}",
            error_type="llm_error",
            status_code=502,
        ) from exc
    except Exception as exc:
        raise AppError(
            code=1021,
            message=f"Embedding 调用异常: {exc}",
            error_type="llm_error",
            status_code=502,
        ) from exc
    try:
        result = json.loads(response_text)
    except Exception as exc:
        raise AppError(
            code=1022,
            message="Embedding 返回不是合法 JSON",
            error_type="llm_error",
            status_code=502,
        ) from exc
    return _extract_embedding_result(result, expected_count=len(texts))


def refresh_semantic_index(data_source_id: str, schema: list[dict[str, Any]]) -> dict[str, Any]:
    init_sqlite_store()
    field_config_map, table_config_map = load_semantic_config_maps(data_source_id)
    documents = build_semantic_schema_documents(
        schema,
        field_config_map=field_config_map,
        table_config_map=table_config_map,
    )
    vectors = embed_texts([doc["semantic_text"] for doc in documents])
    signature = _schema_signature(schema)
    updated_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            "DELETE FROM datasource_semantic_index WHERE data_source_id = ?",
            (data_source_id,),
        )
        for doc, vector in zip(documents, vectors):
            cursor.execute(
                """
                INSERT INTO datasource_semantic_index (
                    data_source_id, table_name, table_comment, semantic_text, aliases_json,
                    embedding_json, embedding_model, embedding_dimension, schema_signature, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data_source_id,
                    doc["table_name"],
                    doc["table_comment"],
                    doc["semantic_text"],
                    json.dumps(doc["aliases"], ensure_ascii=False),
                    json.dumps(vector, ensure_ascii=False),
                    settings.embedding_model,
                    len(vector),
                    signature,
                    updated_at,
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return {
        "refreshed": True,
        "indexed_tables": len(documents),
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.embedding_dimension,
        "updated_at": updated_at,
    }


def get_semantic_index_status(data_source_id: str) -> dict[str, Any]:
    init_sqlite_store()
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) AS indexed_tables, MAX(updated_at) AS updated_at
            FROM datasource_semantic_index
            WHERE data_source_id = ?
            """,
            (data_source_id,),
        )
        row = cursor.fetchone()
    finally:
        connection.close()
    indexed_tables = int(row["indexed_tables"]) if row and row["indexed_tables"] is not None else 0
    updated_at = row["updated_at"] if row else None
    return {
        "vectorized": indexed_tables > 0,
        "indexed_tables": indexed_tables,
        "updated_at": updated_at,
    }


def _load_semantic_index(data_source_id: str) -> list[dict[str, Any]]:
    init_sqlite_store()
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT table_name, table_comment, aliases_json, embedding_json, updated_at
            FROM datasource_semantic_index
            WHERE data_source_id = ?
            ORDER BY table_name
            """,
            (data_source_id,),
        )
        rows = cursor.fetchall()
    finally:
        connection.close()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        result.append(
            {
                "table_name": item["table_name"],
                "table_comment": item["table_comment"],
                "aliases": json.loads(item["aliases_json"]),
                "embedding": json.loads(item["embedding_json"]),
                "updated_at": item["updated_at"],
            }
        )
    return result


def _cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    if not vec1 or not vec2:
        return 0.0
    if len(vec1) != len(vec2):
        return 0.0
    dot = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(b * b for b in vec2))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    return dot / (norm1 * norm2)


def _keyword_boost(question: str, table_name: str, table_comment: str, aliases: list[str]) -> float:
    lowered = question.lower()
    boost = 0.0
    if table_name.lower() in lowered:
        boost += 0.05
    if table_comment and table_comment.lower() in lowered:
        boost += 0.05
    for alias in aliases:
        if alias and alias.lower() in lowered:
            boost += 0.03
    return min(boost, 0.2)


def retrieve_relevant_tables(
    data_source_id: str,
    question: str,
    schema: list[dict[str, Any]],
) -> dict[str, Any]:
    if not settings.semantic_retrieval_enabled:
        return {
            "enabled": False,
            "selected_tables": [],
            "scores": [],
            "fallback_mode": "disabled",
        }
    indexed_rows = _load_semantic_index(data_source_id)
    if not indexed_rows:
        refresh_semantic_index(data_source_id, schema)
        indexed_rows = _load_semantic_index(data_source_id)
    if not indexed_rows:
        return {
            "enabled": True,
            "selected_tables": [],
            "scores": [],
            "fallback_mode": "empty_index",
        }
    question_vector = embed_texts([question])[0]
    scored_items: list[dict[str, Any]] = []
    for row in indexed_rows:
        raw_score = _cosine_similarity(question_vector, row["embedding"])
        boosted_score = raw_score + _keyword_boost(
            question=question,
            table_name=row["table_name"],
            table_comment=row["table_comment"],
            aliases=row["aliases"],
        )
        scored_items.append(
            {
                "table_name": row["table_name"],
                "score": round(boosted_score, 6),
                "raw_score": round(raw_score, 6),
            }
        )
    scored_items.sort(key=lambda item: item["score"], reverse=True)
    top_k = max(1, settings.semantic_top_k)
    threshold_hits = [item for item in scored_items if item["score"] >= settings.semantic_score_threshold]
    fallback_mode = "threshold"
    selected = threshold_hits[:top_k]
    if not selected:
        selected = scored_items[:top_k]
        fallback_mode = "top_k_no_threshold_hit"
    selected_tables = [item["table_name"] for item in selected]
    return {
        "enabled": True,
        "selected_tables": selected_tables,
        "scores": selected,
        "fallback_mode": fallback_mode,
        "top_k": top_k,
        "score_threshold": settings.semantic_score_threshold,
    }
