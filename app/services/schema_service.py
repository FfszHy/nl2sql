from copy import deepcopy
from typing import Any

from app.core.config import settings
from app.core.errors import AppError
from app.models.contracts import DataSourceConfig
from app.services.database import _connect

SCHEMA_METADATA_VERSION = 2


def _attach_relationships(
    grouped: dict[str, dict[str, Any]], rows: list[tuple], schema_name: str,
) -> None:
    """Keep only constraints whose complete keys are visible to this source."""
    visible = {name: {column["name"] for column in table["columns"]} for name, table in grouped.items()}
    foreign_keys = []
    for table_name, kind, columns, target_schema, target_table, target_columns, constraint_name in rows:
        if table_name not in visible or not columns or not set(columns).issubset(visible[table_name]):
            continue
        table = grouped[table_name]
        columns = list(columns)
        if kind in {"p", "u"}:
            table["unique_keys"].append(columns)
            if kind == "p":
                table["primary_key"] = columns
        elif kind == "f" and target_schema == schema_name and target_table in visible:
            if target_columns and set(target_columns).issubset(visible[target_table]):
                foreign_keys.append((table_name, columns, target_schema, target_table, list(target_columns), constraint_name))
    for table_name, columns, target_schema, target_table, target_columns, constraint_name in foreign_keys:
        table = grouped[table_name]
        unique_source = any(set(key).issubset(columns) for key in table["unique_keys"])
        nullable = any(column["nullable"] == "YES" for column in table["columns"] if column["name"] in columns)
        table["foreign_keys"].append({
            "columns": columns, "referenced_schema": target_schema,
            "referenced_table": target_table, "referenced_columns": target_columns,
            "constraint_name": constraint_name,
            "cardinality": "one_to_one" if unique_source else "many_to_one",
            "nullable": nullable,
        })


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
    phase = "字段"
    try:
        connection.read_only = True
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '30s'")
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            phase = "主键、唯一键和外键约束"
            cursor.execute("""
                SELECT rel.relname, con.contype,
                    ARRAY(SELECT attr.attname
                          FROM unnest(con.conkey) WITH ORDINALITY AS key(attnum, position)
                          JOIN pg_catalog.pg_attribute attr
                            ON attr.attrelid = con.conrelid AND attr.attnum = key.attnum
                          ORDER BY key.position),
                    target_ns.nspname, target.relname,
                    ARRAY(SELECT attr.attname
                          FROM unnest(con.confkey) WITH ORDINALITY AS key(attnum, position)
                          JOIN pg_catalog.pg_attribute attr
                            ON attr.attrelid = con.confrelid AND attr.attnum = key.attnum
                          ORDER BY key.position),
                    con.conname
                FROM pg_catalog.pg_constraint con
                JOIN pg_catalog.pg_class rel ON rel.oid = con.conrelid
                JOIN pg_catalog.pg_namespace ns ON ns.oid = rel.relnamespace
                LEFT JOIN pg_catalog.pg_class target ON target.oid = con.confrelid
                LEFT JOIN pg_catalog.pg_namespace target_ns ON target_ns.oid = target.relnamespace
                WHERE ns.nspname = %s AND con.contype IN ('p', 'u', 'f')
                ORDER BY rel.relname, con.conname
            """, [schema_name])
            relationship_rows = cursor.fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for table_name, table_comment, column_name, column_type, is_nullable, column_comment in rows:
            grouped.setdefault(
                table_name,
                {
                    "table_comment": str(table_comment),
                    "columns": [],
                    "primary_key": [], "unique_keys": [], "foreign_keys": [],
                },
            )["columns"].append(
                {
                    "name": str(column_name),
                    "type": str(column_type),
                    "nullable": str(is_nullable),
                    "comment": str(column_comment),
                }
            )
        _attach_relationships(grouped, relationship_rows, schema_name)
        return [
            {
                "table_name": table_name,
                "table_comment": table_data["table_comment"],
                "columns": table_data["columns"],
                "schema_metadata_version": SCHEMA_METADATA_VERSION,
                "primary_key": table_data["primary_key"],
                "unique_keys": table_data["unique_keys"],
                "foreign_keys": table_data["foreign_keys"],
            }
            for table_name, table_data in grouped.items()
        ]
    except AppError:
        raise
    except Exception as exc:
        raise AppError(1009, f"数据库{phase}读取失败: {exc}", "db_error", 400) from exc
    finally:
        connection.close()


def apply_semantic_config(
    schema: list[dict[str, Any]],
    field_config: dict[tuple[str, str], dict[str, Any]],
    table_config: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach configured business language without changing database types or names."""
    enriched = deepcopy(schema)
    for table in enriched:
        table_name = table["table_name"]
        configured_table = table_config.get(table_name, {})
        comment = str(configured_table.get("table_comment") or "").strip()
        if comment:
            table["table_comment"] = comment
        for column in table.get("columns", []):
            configured = field_config.get((table_name, column["name"]), {})
            comment = str(configured.get("field_comment") or "").strip()
            if comment:
                column["comment"] = comment
            column["business_aliases"] = list(configured.get("field_aliases") or [])
    return enriched


def build_schema_context(schema: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for table in schema:
        columns_text = []
        for col in table.get("columns", []):
            aliases = col.get("business_aliases") or []
            aliases_text = f", business_aliases={aliases}" if aliases else ""
            columns_text.append(
                f"{col['name']}({col.get('type', '')}, nullable={col.get('nullable', '')}, "
                f"comment={col.get('comment', '')}{aliases_text})"
            )
        parts.append(
            f"表 {table['table_name']}(comment={table.get('table_comment', '')}): "
            + ", ".join(columns_text)
        )
        if table.get("primary_key"):
            parts.append(f"  数据库主键：{table['primary_key']}")
        if table.get("unique_keys"):
            parts.append(f"  数据库唯一键：{table['unique_keys']}")
        for foreign_key in table.get("foreign_keys", []):
            pairs = ", ".join(
                f"{table['table_name']}.{left} = {foreign_key['referenced_table']}.{right}"
                for left, right in zip(foreign_key["columns"], foreign_key["referenced_columns"])
            )
            parts.append(f"  数据库外键：{pairs}；基数={foreign_key['cardinality']}，外键允许空值={foreign_key['nullable']}")
    return "\n".join(parts)
