import re
from dataclasses import dataclass

from app.core.errors import AppError


FORBIDDEN_KEYWORDS = [
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "truncate",
    "create",
    "replace",
    "grant",
    "revoke",
    "into",
]

FORBIDDEN_FUNCTIONS = [
    "pg_sleep",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "dblink",
    "nextval",
    "setval",
    "pg_advisory_lock",
    "pg_advisory_lock_shared",
    "pg_try_advisory_lock",
    "pg_try_advisory_lock_shared",
    "pg_advisory_xact_lock",
    "pg_advisory_xact_lock_shared",
    "pg_try_advisory_xact_lock",
    "pg_try_advisory_xact_lock_shared",
]

SYSTEM_SCHEMAS = ["information_schema", "pg_catalog", "pg_toast"]

DANGEROUS_INTENT_PATTERNS = [
    r"删除",
    r"删库",
    r"删表",
    r"清空",
    r"新增",
    r"插入",
    r"更新",
    r"修改",
    r"替换",
    r"创建",
    r"重建",
    r"\bdrop\b",
    r"\btruncate\b",
    r"\bdelete\b",
    r"\binsert\b",
    r"\bupdate\b",
    r"\balter\b",
    r"\bcreate\b",
    r"\breplace\b",
]


@dataclass
class SafetyChecks:
    is_select_only: bool
    has_single_statement: bool
    limit_applied: bool

    def to_dict(self) -> dict[str, bool]:
        return {
            "is_select_only": self.is_select_only,
            "has_single_statement": self.has_single_statement,
            "limit_applied": self.limit_applied,
        }


def _ensure_single_statement(sql: str) -> bool:
    trimmed = sql.strip()
    inner = trimmed[:-1] if trimmed.endswith(";") else trimmed
    return ";" not in inner


def _ensure_select_only(sql: str) -> bool:
    return bool(re.match(r"^\s*select\b", sql, flags=re.IGNORECASE))


def _check_forbidden(sql: str) -> None:
    lowered = sql.lower()
    # This validator is deliberately conservative. Reject comments so an
    # appended LIMIT cannot be swallowed by a trailing SQL comment.
    if any(marker in lowered for marker in ("--", "/*", "*/")):
        raise AppError(
            code=2007,
            message="SQL 不允许包含注释",
            error_type="sql_security_error",
            status_code=400,
        )
    for keyword in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", lowered):
            raise AppError(
                code=2001,
                message=f"SQL 包含禁止关键字: {keyword}",
                error_type="sql_security_error",
                status_code=400,
            )
    for fn in FORBIDDEN_FUNCTIONS:
        if re.search(rf'\b{fn}"?\s*\(', lowered):
            raise AppError(
                code=2002,
                message=f"SQL 包含禁止函数: {fn}",
                error_type="sql_security_error",
                status_code=400,
            )
    for schema in SYSTEM_SCHEMAS:
        if re.search(rf"\b{schema}\b", lowered):
            raise AppError(
                code=2003,
                message=f"SQL 访问了禁止系统库: {schema}",
                error_type="sql_security_error",
                status_code=400,
            )


def _normalize_limit(sql: str, max_rows: int) -> tuple[str, bool]:
    stripped = sql.strip().rstrip(";").strip()
    limit_pattern = re.compile(
        r"\blimit\s+(\d+)(?:\s+offset\s+(\d+))?\s*$", flags=re.IGNORECASE
    )
    offset_pattern = re.compile(r"\boffset\s+(\d+)\s*$", flags=re.IGNORECASE)
    matched = limit_pattern.search(stripped)
    if matched:
        limit_value = int(matched.group(1))
        offset_value = matched.group(2)
        if limit_value <= max_rows:
            return stripped, False
        suffix = f"LIMIT {max_rows}" + (f" OFFSET {offset_value}" if offset_value else "")
        return f"{stripped[:matched.start()].rstrip()} {suffix}", True
    offset_only = offset_pattern.search(stripped)
    if offset_only:
        prefix = stripped[:offset_only.start()].rstrip()
        return f"{prefix} LIMIT {max_rows} {offset_only.group(0).strip()}", True
    return f"{stripped} LIMIT {max_rows}", True


def extract_used_tables(sql: str) -> list[str]:
    names = re.findall(
        r'\b(?:from|join)\s+["`]?(?:[a-zA-Z0-9_]+\.)?([a-zA-Z0-9_]+)["`]?',
        sql,
        flags=re.IGNORECASE,
    )
    unique: list[str] = []
    for name in names:
        if name not in unique:
            unique.append(name)
    return unique


def extract_used_columns(sql: str) -> list[str]:
    match = re.search(r"^\s*select\s+(.*?)\s+from\s", sql, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    segment = match.group(1)
    if segment.strip() == "*":
        return ["*"]
    parts = [item.strip() for item in segment.split(",")]
    cleaned: list[str] = []
    for part in parts:
        part = re.sub(r"\s+as\s+.+$", "", part, flags=re.IGNORECASE).strip()
        cleaned.append(part)
    return cleaned


def validate_and_normalize_sql(sql: str, max_rows: int) -> tuple[str, SafetyChecks]:
    if not sql.strip():
        raise AppError(
            code=2000,
            message="SQL 为空",
            error_type="sql_security_error",
            status_code=400,
        )
    has_single_statement = _ensure_single_statement(sql)
    if not has_single_statement:
        raise AppError(
            code=2004,
            message="SQL 必须是单语句",
            error_type="sql_security_error",
            status_code=400,
        )
    is_select_only = _ensure_select_only(sql)
    if not is_select_only:
        raise AppError(
            code=2005,
            message="仅允许 SELECT SQL",
            error_type="sql_security_error",
            status_code=400,
        )
    _check_forbidden(sql)
    normalized_sql, limit_applied = _normalize_limit(sql, max_rows=max_rows)
    checks = SafetyChecks(
        is_select_only=is_select_only,
        has_single_statement=has_single_statement,
        limit_applied=limit_applied,
    )
    return normalized_sql, checks


def reject_dangerous_intent(question: str) -> None:
    text = question.strip().lower()
    for pattern in DANGEROUS_INTENT_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            raise AppError(
                code=2006,
                message="检测到危险操作意图，系统仅支持只读查询，已拒绝执行",
                error_type="sql_security_error",
                status_code=400,
            )
