import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone

from app.core.errors import AppError
from app.models.contracts import DataSourceConfig
from app.services.semantic_bootstrap_service import run_semantic_bootstrap
from app.services.sqlite_store import get_connection, init_sqlite_store

_SECRET = os.getenv("DATASOURCE_SECRET_KEY")


def _stream_key() -> bytes:
    if not _SECRET or _SECRET == "replace-with-a-random-local-secret":
        raise AppError(
            code=1013,
            message="请配置独立的 DATASOURCE_SECRET_KEY 后再注册或读取数据源",
            error_type="config_error",
            status_code=503,
        )
    return hashlib.sha256(_SECRET.encode("utf-8")).digest()


def _encrypt_password(password: str) -> str:
    raw = password.encode("utf-8")
    key = _stream_key()
    encrypted = bytes([byte ^ key[index % len(key)] for index, byte in enumerate(raw)])
    return base64.urlsafe_b64encode(encrypted).decode("utf-8")


def _decrypt_password(cipher_text: str) -> str:
    encrypted = base64.urlsafe_b64decode(cipher_text.encode("utf-8"))
    key = _stream_key()
    raw = bytes([byte ^ key[index % len(key)] for index, byte in enumerate(encrypted)])
    return raw.decode("utf-8")


def create_datasource(name: str, datasource: DataSourceConfig) -> dict:
    init_sqlite_store()
    data_source_id = str(uuid.uuid4())
    created_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            INSERT INTO datasources (
                data_source_id, name, created_at, host, port, user,
                password_cipher, database_name, sslmode, allowed_tables_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data_source_id,
                name.strip(),
                created_at,
                datasource.host,
                datasource.port,
                datasource.user,
                _encrypt_password(datasource.password),
                datasource.database,
                datasource.sslmode,
                json.dumps(datasource.allowed_tables or [], ensure_ascii=False),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    result = {
        "data_source_id": data_source_id,
        "name": name.strip(),
        "created_at": created_at,
    }
    warning: str | None = None
    bootstrap_result: dict[str, object] = {
        "auto_configured": False,
        "saved_table_count": 0,
        "saved_field_count": 0,
        "vectorized": False,
        "indexed_tables": 0,
        "updated_at": None,
        "warning": None,
    }
    try:
        bootstrap_result = run_semantic_bootstrap(data_source_id)
    except Exception as exc:
        warning = f"自动语义初始化或向量化失败: {exc}"
        bootstrap_result["warning"] = warning
    result["semantic_bootstrap"] = bootstrap_result
    return result


def _require_record(data_source_id: str) -> dict:
    init_sqlite_store()
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM datasources WHERE data_source_id = ?", (data_source_id,))
        row = cursor.fetchone()
    finally:
        connection.close()
    if row is None:
        raise AppError(
            code=1011,
            message="数据源不存在",
            error_type="validation_error",
            status_code=404,
        )
    return dict(row)


def get_datasource(data_source_id: str) -> dict:
    record = _require_record(data_source_id)
    return {
        "data_source_id": record["data_source_id"],
        "name": record["name"],
        "database": record["database_name"],
        "host": record["host"],
        "port": record["port"],
        "allowed_tables": json.loads(record["allowed_tables_json"]) if record["allowed_tables_json"] else None,
    }


def get_datasource_config(data_source_id: str) -> DataSourceConfig:
    record = _require_record(data_source_id)
    return DataSourceConfig(
        host=record["host"],
        port=record["port"],
        user=record["user"],
        password=_decrypt_password(record["password_cipher"]),
        database=record["database_name"],
        sslmode=record["sslmode"],
        allowed_tables=json.loads(record["allowed_tables_json"]) if record["allowed_tables_json"] else None,
    )
