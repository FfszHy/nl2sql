"""Validate SQL names against the fetched PostgreSQL schema without rewriting SQL."""

from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import OptimizeError, ParseError, TokenError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.qualify_columns import Resolver, validate_qualify_columns
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.schema import MappingSchema

from app.core.config import settings
from app.core.errors import AppError


def _reject(message: str) -> None:
    raise AppError(1028, message, "schema_validation_error", status_code=422)


def _names(names: Any) -> str:
    values = sorted(set(str(name) for name in names))
    return "、".join(values[:24]) + ("等" if len(values) > 24 else "") or "无"


def _source(scope: Scope, alias: str) -> exp.Table | Scope | None:
    current: Scope | None = scope
    while current is not None:
        source = current.selected_sources.get(alias)
        if source is not None:
            return source[1]
        if not current.can_be_correlated:
            break
        current = current.parent
    return None


def _scope_columns(scope: Scope) -> list[exp.Column]:
    return [
        column for column in scope.expression.find_all(exp.Column)
        if column.find_ancestor(exp.Select) is scope.expression
    ]


def _check_physical_names(query: exp.Expression, tables: dict[str, dict[str, str]], database_schema: str) -> None:
    for scope in traverse_scope(query):
        for _, source in scope.selected_sources.values():
            if not isinstance(source, exp.Table):
                continue
            if not isinstance(source.this, exp.Identifier):
                _reject("暂无法根据已获取的 Schema 核对表函数来源；请使用可核对的真实表或只读 CTE。")
            if source.catalog or (source.db and source.db != database_schema):
                _reject(f"表 {source.sql(dialect='postgres')} 不属于已获取的数据库模式 {database_schema}。")
            if source.name not in tables:
                _reject(f"表 {source.name} 不存在于当前可见 Schema；可用表：{_names(tables)}。")
        for column in _scope_columns(scope):
            if not column.table:
                continue
            if column.db and column.db != database_schema:
                _reject(f"字段 {column.sql(dialect='postgres')} 指向未核对的数据库模式。")
            source = _source(scope, column.table)
            if source is None:
                _reject(f"字段 {column.sql(dialect='postgres')} 使用了未定义的表别名 {column.table}；当前可用别名：{_names(scope.selected_sources)}。")
            if isinstance(source, exp.Table):
                available = list(tables[source.name])
                for index, alias in enumerate(source.alias_column_names):
                    if index < len(available):
                        available[index] = alias
            else:
                available = []
            if isinstance(source, exp.Table) and not column.is_star and column.name not in available:
                _reject(
                    f"字段 {column.sql(dialect='postgres')} 不存在；别名 {column.table} 对应真实表 {source.name}，"
                    f"该来源可用字段：{_names(available)}。请依据真实 Schema 重新生成，不要猜测字段。"
                )


def _legal_output_alias(column: exp.Column, query: exp.Select) -> bool:
    parent = column.parent
    if isinstance(parent, exp.Group) and parent.parent is query:
        return True
    if isinstance(parent, exp.Ordered) and isinstance(parent.parent, exp.Order):
        return parent.parent.parent is query
    if isinstance(parent, exp.Tuple) and isinstance(parent.parent, exp.Distinct):
        return parent.parent.parent is query
    return False


def _check_unqualified_names(query: exp.Expression, schema: MappingSchema) -> None:
    """SQLGlot omits HAVING aliases from its default unresolved-column check."""
    for scope in traverse_scope(query):
        if not isinstance(scope.expression, exp.Select):
            continue
        select = scope.expression
        resolver = Resolver(scope, schema, infer_schema=False)
        input_columns = resolver.all_columns
        outputs: dict[str, list[exp.Expression]] = {}
        for projection in select.selects:
            outputs.setdefault(projection.alias_or_name, []).append(projection)
        for column in _scope_columns(scope):
            if column.table or column.is_star:
                continue
            if column.name in input_columns:
                matches = [alias for alias in scope.selected_sources if column.name in resolver.get_source_columns(alias)]
                if len(matches) > 1:
                    _reject(f"字段 {column.name} 同时存在于多个来源（{_names(matches)}），请将列限定到正确的真实表别名。")
                continue
            projections = outputs.get(column.name, [])
            if projections and _legal_output_alias(column, select):
                if len(projections) != 1:
                    _reject(f"输出别名 {column.name} 重复，无法明确引用；请为输出列使用不同别名。")
                if isinstance(column.parent, exp.Group):
                    projection = projections[0]
                    column.replace((projection.this if isinstance(projection, exp.Alias) else projection).copy())
                continue
            if projections:
                _reject(
                    f"输出别名 {column.name} 不能在此位置作为输入字段引用（例如 WHERE、HAVING 或同层窗口表达式）；"
                    "请用 CTE/子查询先形成该列，再在外层引用。"
                )
            _reject(f"字段 {column.name} 无法在当前来源中解析；可用输入字段：{_names(input_columns)}。请根据真实 Schema 重新生成。")


def validate_schema_sql(
    sql: str,
    schema: list[dict[str, Any]],
    database_schema: str | None = None,
) -> None:
    """Call after the separate single-statement/read-only safety check.

    This checks table, column and alias names, not PostgreSQL function signatures,
    grouping legality, metric semantics or join cardinality. SQL is never modified.
    """
    database_schema = database_schema or settings.pg_schema
    tables = {
        str(table["table_name"]): {str(column["name"]): "UNKNOWN" for column in table.get("columns", [])}
        for table in schema
    }
    try:
        query = normalize_identifiers(parse_one(sql, read="postgres"), dialect="postgres")
        _check_physical_names(query, tables, database_schema)
        known_schema = MappingSchema({database_schema: tables}, dialect="postgres", normalize=False)
        qualified = qualify(
            query.copy(), dialect="postgres", db=database_schema, schema=known_schema,
            infer_schema=False, expand_alias_refs=False, validate_qualify_columns=False,
            quote_identifiers=False, identify=False,
        )
        _check_unqualified_names(qualified, known_schema)
        validate_qualify_columns(qualified)
    except AppError:
        raise
    except OptimizeError as exc:
        _reject(f"SQL 字段或别名不能依据真实 Schema 解析：{exc}。请检查字段是否存在，并将歧义列限定到正确表别名。")
    except (ParseError, TokenError) as exc:
        _reject(f"无法解析 SQL 以核对真实 Schema：{exc}。")
