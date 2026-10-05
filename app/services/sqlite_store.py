import sqlite3
from pathlib import Path

from app.core.config import settings


def _db_path() -> Path:
    path = Path(settings.datasource_sqlite_path)
    if not path.is_absolute():
        root_dir = Path(__file__).resolve().parents[2]
        path = root_dir / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(_db_path())
    connection.row_factory = sqlite3.Row
    return connection


def _migrate_legacy_datasource_schema(cursor: sqlite3.Cursor) -> None:
    """一次性迁移：MySQL 时代的数据源表使用 charset 列，PostgreSQL 改用 sslmode。

    检测到旧结构时直接重建数据源相关表，清空已失效的 MySQL 数据源注册。
    """
    row = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='datasources'"
    ).fetchone()
    if row is None:
        return
    columns = {item[1] for item in cursor.execute("PRAGMA table_info(datasources)").fetchall()}
    if "charset" not in columns or "sslmode" in columns:
        return
    for table in (
        "datasource_semantic_table_config",
        "datasource_semantic_field_config",
        "datasource_semantic_index",
        "datasource_schema_cache",
        "datasources",
    ):
        cursor.execute(f"DROP TABLE IF EXISTS {table}")


def init_sqlite_store() -> None:
    connection = get_connection()
    try:
        cursor = connection.cursor()
        _migrate_legacy_datasource_schema(cursor)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS datasources (
                data_source_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER NOT NULL,
                user TEXT NOT NULL,
                password_cipher TEXT NOT NULL,
                database_name TEXT NOT NULL,
                sslmode TEXT NOT NULL,
                allowed_tables_json TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS datasource_schema_cache (
                data_source_id TEXT PRIMARY KEY,
                schema_json TEXT NOT NULL,
                table_count INTEGER NOT NULL,
                refreshed_at TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS datasource_semantic_index (
                data_source_id TEXT NOT NULL,
                table_name TEXT NOT NULL,
                table_comment TEXT NOT NULL,
                semantic_text TEXT NOT NULL,
                aliases_json TEXT NOT NULL,
                embedding_json TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                embedding_dimension INTEGER NOT NULL,
                schema_signature TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (data_source_id, table_name)
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_semantic_index_datasource
            ON datasource_semantic_index (data_source_id)
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS datasource_semantic_field_config (
                data_source_id TEXT NOT NULL,
                table_name TEXT NOT NULL,
                column_name TEXT NOT NULL,
                field_comment TEXT NOT NULL,
                field_aliases_json TEXT NOT NULL,
                field_type TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (data_source_id, table_name, column_name)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS datasource_semantic_table_config (
                data_source_id TEXT NOT NULL,
                table_name TEXT NOT NULL,
                table_comment TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (data_source_id, table_name)
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_semantic_field_config_datasource
            ON datasource_semantic_field_config (data_source_id)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_semantic_table_config_datasource
            ON datasource_semantic_table_config (data_source_id)
            """
        )
        connection.commit()
    finally:
        connection.close()
