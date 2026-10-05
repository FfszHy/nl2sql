import time
from typing import Any

import psycopg

from app.core.errors import AppError
from app.models.contracts import DataSourceConfig


def _ensure_connect_params(datasource: DataSourceConfig) -> None:
    host = datasource.host.strip().lower()
    user = datasource.user.strip()
    if host.endswith(".pooler.supabase.com") and "." not in user:
        raise AppError(
            code=1002,
            message=(
                "Supabase 连接信息不完整: Host 为 Pooler 地址 (*.pooler.supabase.com) 时, "
                f"User 必须形如 postgres.<项目ref> (当前为 '{user}'), 项目 ref 可在 "
                "Supabase 控制台 Connect / Project Settings → Database 中查看; "
                "Database 一般应填 postgres"
            ),
            error_type="validation_error",
            status_code=400,
        )


def _connect(datasource: DataSourceConfig) -> psycopg.Connection:
    _ensure_connect_params(datasource)
    try:
        return psycopg.connect(
            host=datasource.host,
            port=datasource.port,
            user=datasource.user,
            password=datasource.password,
            dbname=datasource.database,
            sslmode=datasource.sslmode,
            autocommit=False,
        )
    except Exception as exc:
        raise AppError(
            code=1002,
            message=f"数据库连接失败: {exc}",
            error_type="db_error",
            status_code=400,
        ) from exc


def test_connection(datasource: DataSourceConfig) -> dict[str, Any]:
    begin = time.perf_counter()
    connection = _connect(datasource)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SHOW server_version")
            server_version_row = cursor.fetchone()
            cursor.execute("SELECT 1")
            cursor.fetchone()
        latency_ms = int((time.perf_counter() - begin) * 1000)
        server_version = (
            str(server_version_row[0]) if server_version_row and server_version_row[0] else ""
        )
        return {
            "success": True,
            "latency_ms": latency_ms,
            "server_version": server_version,
            "database": datasource.database,
        }
    finally:
        connection.close()


def execute_select_sql(
    datasource: DataSourceConfig,
    sql: str,
) -> tuple[list[str], list[list[Any]], int]:
    connection = _connect(datasource)
    try:
        # SELECT can invoke functions with side effects. Enforce read-only
        # transactions at the database layer as well as filtering generated SQL.
        connection.read_only = True
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '30s'")
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            cursor.execute(sql)
            rows_raw = cursor.fetchall()
            description = cursor.description or []
            columns = [col.name for col in description]
            rows = [list(item) for item in rows_raw]
            connection.rollback()
            return columns, rows, len(rows)
    except Exception as exc:
        connection.rollback()
        raise AppError(
            code=1003,
            message=f"SQL 执行失败: {exc}",
            error_type="db_error",
            status_code=400,
        ) from exc
    finally:
        connection.close()
