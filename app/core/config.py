import os
from pathlib import Path


def _load_dotenv() -> None:
    root_dir = Path(__file__).resolve().parents[2]
    dotenv_path = root_dir / ".env"
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv()


def _to_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _to_float(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _to_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _to_list(value: str | None, default: list[str]) -> list[str]:
    if value is None:
        return default
    items = [item.strip() for item in value.split(",")]
    items = [item for item in items if item]
    return items or default


class Settings:
    app_name: str = "smart-nl2sql-api"
    llm_api_url: str = os.getenv("LLM_API_URL") or os.getenv(
        "DASHSCOPE_BASE_URL",
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
    )
    llm_api_key: str | None = os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    llm_model: str = os.getenv("LLM_MODEL") or os.getenv("DASHSCOPE_MODEL", "qwen-plus")
    llm_auth_header: str = os.getenv("LLM_AUTH_HEADER", "Authorization")
    llm_auth_scheme: str = os.getenv("LLM_AUTH_SCHEME", "Bearer")
    llm_extra_headers_json: str = os.getenv("LLM_EXTRA_HEADERS_JSON", "{}")
    llm_payload_model_path: str = os.getenv("LLM_PAYLOAD_MODEL_PATH", "model")
    llm_payload_messages_path: str = os.getenv("LLM_PAYLOAD_MESSAGES_PATH", "input.messages")
    llm_payload_extra_json: str = os.getenv(
        "LLM_PAYLOAD_EXTRA_JSON",
        '{"parameters":{"result_format":"message"}}',
    )
    llm_response_content_paths: str = os.getenv(
        "LLM_RESPONSE_CONTENT_PATHS",
        "output.choices.0.message.content,output.text",
    )
    llm_timeout_seconds: int = _to_int(os.getenv("LLM_TIMEOUT_SECONDS"), 120)
    embedding_api_url: str = os.getenv(
        "EMBEDDING_API_URL",
        "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
    )
    embedding_api_key: str | None = os.getenv("EMBEDDING_API_KEY") or os.getenv(
        "DASHSCOPE_API_KEY"
    ) or llm_api_key
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "text-embedding-v4")
    embedding_dimension: int = _to_int(os.getenv("EMBEDDING_DIMENSION"), 1024)
    embedding_timeout_seconds: int = _to_int(os.getenv("EMBEDDING_TIMEOUT_SECONDS"), 60)
    semantic_retrieval_enabled: bool = _to_bool(
        os.getenv("SEMANTIC_RETRIEVAL_ENABLED"),
        True,
    )
    semantic_top_k: int = _to_int(os.getenv("SEMANTIC_TOP_K"), 3)
    semantic_score_threshold: float = _to_float(
        os.getenv("SEMANTIC_SCORE_THRESHOLD"),
        0.55,
    )
    datasource_sqlite_path: str = os.getenv("DATASOURCE_SQLITE_PATH", "data/app_state.sqlite3")
    schema_cache_ttl_seconds: int = _to_int(os.getenv("SCHEMA_CACHE_TTL_SECONDS"), 600)
    pg_schema: str = os.getenv("PG_SCHEMA", "public")
    cors_allow_origins: list[str] = _to_list(
        os.getenv("CORS_ALLOW_ORIGINS"),
        ["http://127.0.0.1:5173", "http://localhost:5173"],
    )
    # Backward-compatible aliases.
    dashscope_api_key: str | None = llm_api_key
    dashscope_base_url: str = llm_api_url
    dashscope_model: str = llm_model


settings = Settings()
