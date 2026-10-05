from typing import Any

from app.core.config import settings
from app.models.contracts import DataSourceConfig
from app.services.database import _connect


def fetch_schema(datasource: DataSourceConfig) -> list[dict[str, Any]]:
    schema_name = settings.pg_schema
    table_filter_sql = ""
    params: list[Any] = [schema_name]
    if datasource.allowed_tables:
        placeholders = ",".join(["%s"] * len(datasource.allowed_tables))
        table_filter_sql = f" AND c.table_name IN ({placeholders})"
        params.extend(datasource.allowed_tables)
    sql = f"""
        SELECT
            c.table_name,
            COALESCE(
                obj_description(
                    (quote_ident(c.table_schema) || '.' || quote_ident(c.table_name))::regclass,
                    'pg_class'
                ),
                ''
            ) AS table_comment,
            c.column_name,
            CASE
                WHEN c.data_type = 'character varying'
                    THEN 'varchar(' || COALESCE(c.character_maximum_length::text, '') || ')'
                WHEN c.data_type = 'numeric'
                    THEN 'numeric(' || COALESCE(c.numeric_precision::text, '')
                         || ',' || COALESCE(c.numeric_scale::text, '') || ')'
                ELSE c.data_type
            END AS column_type,
            c.is_nullable,
            COALESCE(
                col_description(
                    (quote_ident(c.table_schema) || '.' || quote_ident(c.table_name))::regclass,
                    c.ordinal_position
                ),
                ''
            ) AS column_comment
        FROM information_schema.columns c
        JOIN information_schema.tables t
            ON t.table_schema = c.table_schema
            AND t.table_name = c.table_name
        WHERE c.table_schema = %s
          AND t.table_type = 'BASE TABLE'
        {table_filter_sql}
        ORDER BY c.table_name, c.ordinal_position
    """
    connection = _connect(datasource)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for table_name, table_comment, column_name, column_type, is_nullable, column_comment in rows:
            grouped.setdefault(
                table_name,
                {
                    "table_comment": str(table_comment),
                    "columns": [],
                },
            )["columns"].append(
                {
                    "name": str(column_name),
                    "type": str(column_type),
                    "nullable": str(is_nullable),
                    "comment": str(column_comment),
                }
            )
        return [
            {
                "table_name": table_name,
                "table_comment": table_data["table_comment"],
                "columns": table_data["columns"],
            }
            for table_name, table_data in grouped.items()
        ]
    finally:
        connection.close()


def build_schema_context(schema: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for table in schema:
        columns_text = []
        for col in table["columns"]:
            columns_text.append(
                f"{col['name']}({col['type']}, nullable={col['nullable']}, comment={col['comment']})"
            )
        parts.append(
            f"表 {table['table_name']}(comment={table.get('table_comment', '')}): "
            + ", ".join(columns_text)
        )
    return "\n".join(parts)