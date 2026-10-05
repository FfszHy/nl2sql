import json
import time
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.models.contracts import DataSourceConfig
from app.services.schema_service import fetch_schema
from app.services.sqlite_store import get_connection, init_sqlite_store


def _cache_row(data_source_id: str) -> dict[str, Any] | None:
    init_sqlite_store()
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT * FROM datasource_schema_cache WHERE data_source_id = ?",
            (data_source_id,),
        )
        row = cursor.fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return dict(row)


def _save_cache(data_source_id: str, schema: list[dict[str, Any]]) -> dict[str, Any]:
    init_sqlite_store()
    refreshed_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    expires_at = int(time.time()) + settings.schema_cache_ttl_seconds
    table_count = len(schema)
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            INSERT INTO datasource_schema_cache (
                data_source_id, schema_json, table_count, refreshed_at, expires_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(data_source_id) DO UPDATE SET
                schema_json=excluded.schema_json,
                table_count=excluded.table_count,
                refreshed_at=excluded.refreshed_at,
                expires_at=excluded.expires_at
            """,
            (
                data_source_id,
                json.dumps(schema, ensure_ascii=False),
                table_count,
                refreshed_at,
                expires_at,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "refreshed": True,
        "table_count": table_count,
        "refreshed_at": refreshed_at,
    }


def refresh_schema_cache(data_source_id: str) -> dict[str, Any]:
    from app.services.datasource_registry import get_datasource_config

    datasource = get_datasource_config(data_source_id)
    schema = fetch_schema(datasource)
    return _save_cache(data_source_id, schema)


def fetch_schema_with_cache(data_source_id: str) -> tuple[DataSourceConfig, list[dict[str, Any]]]:
    from app.services.datasource_registry import get_datasource_config

    datasource = get_datasource_config(data_source_id)
    row = _cache_row(data_source_id)
    now_ts = int(time.time())
    if row and row["expires_at"] >= now_ts:
        return datasource, json.loads(row["schema_json"])
    schema = fetch_schema(datasource)
    _save_cache(data_source_id, schema)
    return datasource, schema
