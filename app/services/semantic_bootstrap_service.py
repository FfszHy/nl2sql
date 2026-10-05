from typing import Any

from app.services.schema_cache_service import fetch_schema_with_cache, refresh_schema_cache
from app.services.semantic_config_service import bootstrap_semantic_config
from app.services.semantic_retrieval_service import refresh_semantic_index


def run_semantic_bootstrap(data_source_id: str) -> dict[str, Any]:
    refresh_schema_cache(data_source_id)
    _, schema = fetch_schema_with_cache(data_source_id)
    config_result = bootstrap_semantic_config(data_source_id, schema)
    index_result = refresh_semantic_index(data_source_id, schema)
    return {
        "auto_configured": True,
        "saved_table_count": config_result["saved_table_count"],
        "saved_field_count": config_result["saved_field_count"],
        "vectorized": True,
        "indexed_tables": index_result["indexed_tables"],
        "updated_at": index_result["updated_at"],
        "warning": None,
    }
