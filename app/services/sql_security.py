import re
from dataclasses import dataclass

from sqlglot import ErrorLevel, exp, parse
from sqlglot.errors import ParseError, TokenError, UnsupportedError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, traverse_scope

from app.core.errors import AppError


FORBIDDEN_KEYWORDS = [
    "insert",
    "update",
    "delete",
    "merge",
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


def _is_select_query(query: exp.Expression) -> bool:
    if isinstance(query, exp.Subquery):
        return _is_select_query(query.this)
    if isinstance(query, exp.SetOperation):
        return _is_select_query(query.this) and _is_select_query(query.expression)
    return isinstance(query, exp.Select)


def _parse_select_query(sql: str) -> exp.Query:
    try:
        statements = [item for item in parse(sql, read="postgres", error_level=ErrorLevel.RAISE) if item is not None]
    except (ParseError, TokenError) as exc:
        raise AppError(
            code=2008,
            message="生成的 SQL 无法解析，请重试",
            error_type="sql_security_error",
            status_code=400,
        ) from exc
    if len(statements) != 1:
        raise AppError(2004, "SQL 必须是单语句", "sql_security_error", 400)
    query = statements[0]
    if (
        query is None
        or not _is_select_query(query)
        or any(not _is_select_query(cte.this) for cte in query.find_all(exp.CTE))
        or any(isinstance(node, (exp.DML, exp.DDL, exp.Command)) for node in query.walk())
    ):
        raise AppError(
            code=2005,
            message="仅允许只读 SELECT SQL（可包含只读 WITH/CTE）",
            error_type="sql_security_error",
            status_code=400,
        )
    return query


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


def _quote_source_aliases(query: exp.Query) -> exp.Query:
    """Quote source aliases without changing PostgreSQL identifier resolution.

    SQLGlot accepts some unquoted aliases that PostgreSQL reserves. Fold an
    unquoted alias before quoting it, and retain the case of quoted aliases.
    Resolve CTE references by scope so a same-named physical table remains a
    physical table. Field names, functions and literals are not rewritten.
    """
    # Normalize relation names for scope lookup, including unquoted CTE names.
    # PostgreSQL folds these names even when their spelling uses upper case.
    for table in query.find_all(exp.Table):
        if isinstance(table.this, exp.Identifier):
            normalize_identifiers(table.this, dialect="postgres")
    for alias in query.find_all(exp.TableAlias):
        if isinstance(alias.this, exp.Identifier):
            normalize_identifiers(alias.this, dialect="postgres")
            alias.this.set("quoted", True)
    for column in query.find_all(exp.Column):
        qualifier = column.args.get("table")
        if isinstance(qualifier, exp.Identifier):
            normalize_identifiers(qualifier, dialect="postgres")
            qualifier.set("quoted", True)
    for scope in traverse_scope(query):
        for name, reference in scope.references:
            source = scope.sources.get(name)
            if isinstance(source, Scope) and isinstance(reference, exp.Table) and isinstance(reference.this, exp.Identifier):
                reference.this.set("quoted", True)
    return query


def _normalize_limit(sql: str, max_rows: int, query: exp.Query) -> tuple[str, bool]:
    # Only the outer query limit bounds returned rows. A LIMIT inside a CTE
    # or one branch of a UNION must not stand in for the outer result cap.
    limit = query.args.get("limit")
    value = None
    if isinstance(limit, exp.Limit):
        value = limit.expression
    elif isinstance(limit, exp.Fetch):
        value = limit.args.get("count")
    while isinstance(value, exp.Paren):
        value = value.this
    options = limit.args.get("limit_options") if isinstance(limit, exp.Fetch) else None
    has_ties_or_percent = bool(
        options and (options.args.get("with_ties") or options.args.get("percent"))
    )
    is_numeric_limit = isinstance(value, exp.Literal) and value.is_int and int(value.this) >= 0
    if is_numeric_limit and int(value.this) <= max_rows and not has_ties_or_percent:
        capped, limit_applied = query, False
    elif limit and (has_ties_or_percent or (not is_numeric_limit and not isinstance(value, exp.Null))):
        # Preserve expression limits and FETCH WITH TIES before applying the
        # result cap; replacing them could increase a deliberately smaller limit.
        capped, limit_applied = exp.select("*").from_(query.subquery("_limited_result")).limit(max_rows), True
    else:
        capped, limit_applied = query.limit(max_rows), True
    try:
        return _quote_source_aliases(capped).sql(dialect="postgres", unsupported_level=ErrorLevel.RAISE), limit_applied
    except UnsupportedError as exc:
        raise AppError(
            code=2008,
            message="生成的 SQL 包含暂不支持的语法，请重试",
            error_type="sql_security_error",
            status_code=400,
        ) from exc


def extract_used_tables(sql: str) -> list[str]:
    query = _parse_select_query(sql)
    unique: list[str] = []
    for scope in traverse_scope(query):
        for source in scope.sources.values():
            # A CTE reference resolves to a Scope; a physical table resolves
            # to a Table, even when it shares a name with a CTE elsewhere.
            if isinstance(source, exp.Table) and source.name not in unique:
                unique.append(source.name)
    return unique


def extract_used_columns(sql: str) -> list[str]:
    query = _parse_select_query(sql)
    return [
        (item.this if isinstance(item, exp.Alias) else item).sql(dialect="postgres")
        for item in query.selects
    ]


def validate_and_normalize_sql(sql: str, max_rows: int) -> tuple[str, SafetyChecks]:
    if not sql.strip():
        raise AppError(
            code=2000,
            message="SQL 为空",
            error_type="sql_security_error",
            status_code=400,
        )
    query = _parse_select_query(sql)
    _check_forbidden(sql)
    normalized_sql, limit_applied = _normalize_limit(sql, max_rows=max_rows, query=query)
    checks = SafetyChecks(
        is_select_only=True,
        has_single_statement=True,
        limit_applied=limit_applied,
    )
    return normalized_sql, checks


def can_repair_readonly_output(sql: str, error_code: int) -> bool:
    """Only regenerate stacked, independently safe SELECTs; never execute any of them."""
    if error_code != 2004:
        return False
    try:
        _check_forbidden(sql)
        statements = [item for item in parse(sql, read="postgres", error_level=ErrorLevel.RAISE) if item is not None]
        return len(statements) > 1 and all(
            _is_select_query(query)
            and all(_is_select_query(cte.this) for cte in query.find_all(exp.CTE))
            and not any(isinstance(node, (exp.DML, exp.DDL, exp.Command)) for node in query.walk())
            for query in statements
        )
    except (AppError, ParseError, TokenError):
        return False


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
