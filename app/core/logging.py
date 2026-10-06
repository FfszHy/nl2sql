import json
import logging
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from app.core.config import settings


# Query diagnostics have an explicit field boundary: callers cannot accidentally
# persist a result preview, datasource object, password or request headers.
_AUDIT_FIELDS = {
    "trace_id", "question", "sql", "candidate_sql", "error_type", "error_code",
    "error_message", "phase", "sqlstate", "attempt", "attempt_count", "elapsed_ms",
    "retry_used", "meta_answered", "row_count", "include_chart", "rewritten_question",
    "few_shot_categories", "mapping_count", "safety_checks", "used_tables", "used_columns",
    "business_profile_ids", "business_metric_ids", "table_count", "data_source_id",
    "method", "path", "status_code",
    "business_failure_reason", "business_failure_details", "chart_type", "chart_intent", "chart_reason",
}


def _clean_value(value: Any) -> Any:
    if isinstance(value, str):
        value = re.sub(r"(?i)\b(password|passwd|pwd|api[_-]?key|llm_api_key|embedding_api_key|datasource_secret_key)\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)", r"\1=[REDACTED]", value)
        value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", value)
        return re.sub(r"(?i)(postgres(?:ql)?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", value)
    if isinstance(value, dict):
        return {key: _clean_value(item) for key, item in value.items() if str(key).lower() not in {
            "rows", "rows_preview", "datasource", "credentials", "headers", "authorization",
        } and not any(token in str(key).lower() for token in ("password", "secret", "api_key", "token"))}
    if isinstance(value, (list, tuple)):
        return [_clean_value(item) for item in value]
    return value if value is None or isinstance(value, (bool, int, float)) else f"<{type(value).__name__}>"

def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    if name == "app.audit.query" and settings.query_audit_path.strip():
        try:
            path = Path(settings.query_audit_path)
            if not path.is_absolute():
                path = Path(__file__).resolve().parents[2] / path
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                path, maxBytes=settings.query_audit_max_bytes,
                backupCount=settings.query_audit_backup_count, encoding="utf-8",
            )
            file_handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(file_handler)
        except (OSError, ValueError) as exc:
            audit_log(logger, "audit_file_unavailable", phase="logging", error_message=str(exc))
    return logger


def audit_log(logger: logging.Logger, event: str, **fields: Any) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event": event,
        **{key: _clean_value(value) for key, value in fields.items() if key in _AUDIT_FIELDS},
    }
    # Rotation alone does not bound a single oversized record. Keep valid JSON
    # and identify omitted diagnostics instead of letting one SQL fill the disk.
    budget = max(1024, min(settings.query_audit_max_bytes - 1, 64 * 1024))
    essential = {"timestamp", "event", "trace_id", "error_code", "error_type", "phase", "sqlstate"}
    truncated = []
    while True:
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) < budget:
            break
        candidates = [key for key in payload if key not in essential and key != "truncated_fields"]
        if not candidates:
            # Trace IDs and event names are normally short, but still bound
            # malformed caller metadata before emitting a valid record.
            for key in essential:
                if isinstance(payload.get(key), str):
                    payload[key] = payload[key][:128]
            encoded = json.dumps(payload, ensure_ascii=False, default=str)
            break
        key = max(candidates, key=lambda name: len(json.dumps(payload[name], ensure_ascii=False, default=str).encode("utf-8")))
        value = payload[key]
        if isinstance(value, str) and len(value.encode("utf-8")) > 256:
            payload[key] = value.encode("utf-8")[:256].decode("utf-8", errors="ignore") + " [truncated]"
        else:
            del payload[key]
        if key not in truncated:
            truncated.append(key)
        payload["truncated_fields"] = truncated
    logger.info(encoded)
