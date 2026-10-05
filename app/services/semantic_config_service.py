import json
import re
from datetime import datetime, timezone
from typing import Any

from app.core.errors import AppError
from app.models.contracts import SemanticFieldConfigItem, SemanticTableConfigItem
from app.services.schema_cache_service import fetch_schema_with_cache, refresh_schema_cache
from app.services.sqlite_store import get_connection, init_sqlite_store


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _load_field_rows(data_source_id: str) -> list[dict[str, Any]]:
    init_sqlite_store()
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT table_name, column_name, field_comment, field_aliases_json, field_type, updated_at
            FROM datasource_semantic_field_config
            WHERE data_source_id = ?
            ORDER BY table_name, column_name
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
                "column_name": item["column_name"],
                "field_comment": item["field_comment"],
                "field_aliases": json.loads(item["field_aliases_json"]),
                "field_type": item["field_type"],
                "updated_at": item["updated_at"],
            }
        )
    return result


def _load_table_rows(data_source_id: str) -> list[dict[str, Any]]:
    init_sqlite_store()
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT table_name, table_comment, updated_at
            FROM datasource_semantic_table_config
            WHERE data_source_id = ?
            ORDER BY table_name
            """,
            (data_source_id,),
        )
        rows = cursor.fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _schema_column_keys(schema: list[dict[str, Any]]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for table in schema:
        table_name = str(table["table_name"])
        for column in table["columns"]:
            keys.add((table_name, str(column["name"])))
    return keys


def _schema_table_names(schema: list[dict[str, Any]]) -> set[str]:
    return {str(table["table_name"]) for table in schema}


def _schema_table_map(schema: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(table["table_name"]): table for table in schema}


def _split_identifier(text: str) -> list[str]:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    parts = [part.strip() for part in normalized.replace("-", "_").split("_")]
    return [part for part in parts if part]


def _fallback_table_comment(table_name: str, raw_comment: str) -> str:
    value = (raw_comment or "").strip()
    return value or table_name


def _fallback_field_comment(column_name: str, raw_comment: str) -> str:
    value = (raw_comment or "").strip()
    return value or column_name


def _fallback_field_type(raw_type: str) -> str:
    value = (raw_type or "").strip()
    return value or "unknown"


def _fallback_field_aliases(column_name: str, field_comment: str, configured_aliases: list[str]) -> list[str]:
    aliases = [item.strip() for item in configured_aliases if str(item).strip()]
    if aliases:
        return list(dict.fromkeys(aliases))
    generated = [field_comment.strip()]
    generated.extend(_split_identifier(column_name))
    generated.append(column_name.strip())
    return list(dict.fromkeys([item for item in generated if item]))


def _build_default_semantic_payload(
    schema: list[dict[str, Any]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str, list[str], str]]]:
    table_rows: list[tuple[str, str]] = []
    field_rows: list[tuple[str, str, str, list[str], str]] = []
    for table in schema:
        table_name = str(table["table_name"])
        table_comment = _fallback_table_comment(table_name, str(table.get("table_comment") or ""))
        table_rows.append((table_name, table_comment))
        for column in table["columns"]:
            column_name = str(column["name"])
            field_comment = _fallback_field_comment(column_name, str(column.get("comment") or ""))
            field_type = _fallback_field_type(str(column.get("type") or ""))
            aliases = _fallback_field_aliases(column_name, field_comment, [])
            field_rows.append((table_name, column_name, field_comment, aliases, field_type))
    return table_rows, field_rows


def load_semantic_config_maps(
    data_source_id: str,
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, dict[str, Any]]]:
    field_rows = _load_field_rows(data_source_id)
    table_rows = _load_table_rows(data_source_id)
    field_config_map: dict[tuple[str, str], dict[str, Any]] = {}
    table_config_map: dict[str, dict[str, Any]] = {}
    for row in field_rows:
        field_config_map[(row["table_name"], row["column_name"])] = row
    for row in table_rows:
        table_config_map[row["table_name"]] = row
    return field_config_map, table_config_map


def get_semantic_config_schema(data_source_id: str) -> dict[str, Any]:
    _, schema = fetch_schema_with_cache(data_source_id)
    field_config_rows = _load_field_rows(data_source_id)
    table_config_rows = _load_table_rows(data_source_id)
    field_config_map, table_config_map = load_semantic_config_maps(data_source_id)
    tables: list[dict[str, Any]] = []
    missing_field_count = 0
    missing_table_count = 0
    for table in schema:
        table_name = str(table["table_name"])
        raw_table_comment = str(table.get("table_comment") or "")
        table_current = table_config_map.get(table_name)
        table_comment = str(table_current["table_comment"]) if table_current else raw_table_comment
        table_comment_configured = bool(table_current and str(table_current["table_comment"]).strip())
        if not table_comment_configured:
            missing_table_count += 1
        columns: list[dict[str, Any]] = []
        for column in table["columns"]:
            column_name = str(column["name"])
            raw_type = str(column.get("type") or "")
            raw_comment = str(column.get("comment") or "")
            current = field_config_map.get((table_name, column_name))
            if current is None:
                missing_field_count += 1
            columns.append(
                {
                    "column_name": column_name,
                    "required": True,
                    "configured": current is not None,
                    "field_comment": current["field_comment"] if current else raw_comment,
                    "field_aliases": current["field_aliases"] if current else [],
                    "field_type": current["field_type"] if current else raw_type,
                }
            )
        tables.append(
            {
                "table_name": table_name,
                "table_comment": table_comment,
                "table_comment_configured": table_comment_configured,
                "columns": columns,
            }
        )
    return {
        "tables": tables,
        "table_count": len(tables),
        "configured_table_count": len(table_config_rows),
        "missing_table_count": missing_table_count,
        "configured_field_count": len(field_config_rows),
        "missing_field_count": missing_field_count,
        "all_configured": missing_field_count == 0 and missing_table_count == 0,
    }


def save_semantic_config(
    data_source_id: str,
    tables: list[SemanticTableConfigItem],
    fields: list[SemanticFieldConfigItem],
) -> dict[str, Any]:
    _, schema = fetch_schema_with_cache(data_source_id)
    schema_table_map = _schema_table_map(schema)
    valid_keys = _schema_column_keys(schema)
    valid_table_names = _schema_table_names(schema)
    seen_table_names: set[str] = set()
    seen_keys: set[tuple[str, str]] = set()
    upsert_table_rows: list[tuple[str, str, str, str]] = []
    upsert_rows: list[tuple[str, str, str, str, str, str, str]] = []
    now = _now_iso()
    field_schema_map: dict[tuple[str, str], dict[str, Any]] = {}
    for table in schema:
        table_name = str(table["table_name"])
        for column in table["columns"]:
            field_schema_map[(table_name, str(column["name"]))] = column
    for item in tables:
        table_name = item.table_name.strip()
        if table_name in seen_table_names:
            raise AppError(
                code=1024,
                message=f"语义配置存在重复表: {table_name}",
                error_type="validation_error",
                status_code=422,
            )
        seen_table_names.add(table_name)
        if table_name not in valid_table_names:
            raise AppError(
                code=1024,
                message=f"语义配置表不存在于数据源 schema: {table_name}",
                error_type="validation_error",
                status_code=422,
            )
        upsert_table_rows.append(
            (
                data_source_id,
                table_name,
                item.table_comment.strip(),
                now,
            )
        )
    for item in fields:
        table_name = item.table_name.strip()
        column_name = item.column_name.strip()
        key = (table_name, column_name)
        if key in seen_keys:
            raise AppError(
                code=1024,
                message=f"语义配置存在重复字段: {item.table_name}.{item.column_name}",
                error_type="validation_error",
                status_code=422,
            )
        seen_keys.add(key)
        if key not in valid_keys:
            raise AppError(
                code=1024,
                message=f"语义配置字段不存在于数据源 schema: {item.table_name}.{item.column_name}",
                error_type="validation_error",
                status_code=422,
            )
        schema_column = field_schema_map[key]
        field_comment = item.field_comment.strip()
        if not field_comment:
            field_comment = _fallback_field_comment(column_name, str(schema_column.get("comment") or ""))
        field_type = item.field_type.strip()
        if not field_type:
            field_type = _fallback_field_type(str(schema_column.get("type") or ""))
        aliases = _fallback_field_aliases(column_name, field_comment, item.field_aliases)
        upsert_rows.append(
            (
                data_source_id,
                table_name,
                column_name,
                field_comment,
                json.dumps(aliases, ensure_ascii=False),
                field_type,
                now,
            )
        )
    for table_name in valid_table_names:
        if table_name in seen_table_names:
            continue
        raw_comment = str(schema_table_map.get(table_name, {}).get("table_comment") or "")
        upsert_table_rows.append(
            (
                data_source_id,
                table_name,
                _fallback_table_comment(table_name, raw_comment),
                now,
            )
        )
    for key in valid_keys:
        if key in seen_keys:
            continue
        table_name, column_name = key
        schema_column = field_schema_map[key]
        field_comment = _fallback_field_comment(column_name, str(schema_column.get("comment") or ""))
        field_type = _fallback_field_type(str(schema_column.get("type") or ""))
        aliases = _fallback_field_aliases(column_name, field_comment, [])
        upsert_rows.append(
            (
                data_source_id,
                table_name,
                column_name,
                field_comment,
                json.dumps(aliases, ensure_ascii=False),
                field_type,
                now,
            )
        )
    init_sqlite_store()
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            "DELETE FROM datasource_semantic_field_config WHERE data_source_id = ?",
            (data_source_id,),
        )
        cursor.execute(
            "DELETE FROM datasource_semantic_table_config WHERE data_source_id = ?",
            (data_source_id,),
        )
        if upsert_table_rows:
            cursor.executemany(
                """
                INSERT INTO datasource_semantic_table_config (
                    data_source_id, table_name, table_comment, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                upsert_table_rows,
            )
        if upsert_rows:
            cursor.executemany(
                """
                INSERT INTO datasource_semantic_field_config (
                    data_source_id, table_name, column_name, field_comment,
                    field_aliases_json, field_type, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                upsert_rows,
            )
        connection.commit()
    finally:
        connection.close()
    return {
        "saved": True,
        "saved_table_count": len(upsert_table_rows),
        "saved_field_count": len(upsert_rows),
        "updated_at": now,
    }


def bootstrap_semantic_config(data_source_id: str, schema: list[dict[str, Any]]) -> dict[str, Any]:
    default_tables, default_fields = _build_default_semantic_payload(schema)
    now = _now_iso()
    upsert_table_rows: list[tuple[str, str, str, str]] = [
        (data_source_id, table_name, table_comment, now)
        for table_name, table_comment in default_tables
    ]
    upsert_rows: list[tuple[str, str, str, str, str, str, str]] = [
        (
            data_source_id,
            table_name,
            column_name,
            field_comment,
            json.dumps(field_aliases, ensure_ascii=False),
            field_type,
            now,
        )
        for table_name, column_name, field_comment, field_aliases, field_type in default_fields
    ]
    init_sqlite_store()
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            "DELETE FROM datasource_semantic_field_config WHERE data_source_id = ?",
            (data_source_id,),
        )
        cursor.execute(
            "DELETE FROM datasource_semantic_table_config WHERE data_source_id = ?",
            (data_source_id,),
        )
        if upsert_table_rows:
            cursor.executemany(
                """
                INSERT INTO datasource_semantic_table_config (
                    data_source_id, table_name, table_comment, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                upsert_table_rows,
            )
        if upsert_rows:
            cursor.executemany(
                """
                INSERT INTO datasource_semantic_field_config (
                    data_source_id, table_name, column_name, field_comment,
                    field_aliases_json, field_type, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                upsert_rows,
            )
        connection.commit()
    finally:
        connection.close()
    return {
        "saved": True,
        "saved_table_count": len(upsert_table_rows),
        "saved_field_count": len(upsert_rows),
        "updated_at": now,
    }


def refresh_schema_before_config(data_source_id: str) -> None:
    refresh_schema_cache(data_source_id)
