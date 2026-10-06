"""Check configured SQL contracts with bounded provenance and uniqueness proofs."""

from decimal import Decimal, InvalidOperation
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.optimizer.scope import Scope, build_scope

from app.core.config import settings
from app.core.errors import AppError


def _reject(message: str, reason: str | None = None, details: dict[str, Any] | None = None) -> None:
    error = AppError(1027, message, "business_semantic_error", status_code=422)
    if reason:
        error.business_failure_reason = reason
        allowed = {"metric_id", "output_alias", "source_table", "source_aliases", "tie_keys"}
        error.business_failure_details = {
            key: [str(item)[:128] for item in value[:8]] if isinstance(value, list) else str(value)[:128]
            for key, value in (details or {}).items() if key in allowed
        }
    raise error


def _sources(scope: Scope) -> dict[str, exp.Table | Scope]:
    return {name.lower(): source for name, (_, source) in scope.selected_sources.items()}


def _projection(scope: Scope, name: str) -> exp.Expression | None:
    if not isinstance(scope.expression, exp.Select):
        return None
    selects = scope.expression.selects
    outer_names = [str(value).lower() for value in scope.outer_columns]
    if name.lower() in outer_names:
        return selects[outer_names.index(name.lower())]
    matches = [item for item in selects if item.alias_or_name.lower() == name.lower()]
    return matches[0] if len(matches) == 1 else None


def _expand_column(column: exp.Column, scope: Scope, trail: set[tuple[int, str]]) -> exp.Expression:
    """Resolve aliases through projected columns; ambiguous unqualified columns stay unknown."""
    sources = _sources(scope)
    source = sources.get(column.table.lower()) if column.table else None
    if not column.table and len(sources) == 1:
        source = next(iter(sources.values()))
    if source is None and column.table and scope.parent is not None:
        return _expand_column(column, scope.parent, trail)
    if isinstance(source, exp.Table):
        bound = exp.column(column.name.lower(), table=source.name.lower())
        alias = column.table.lower() if column.table else next(iter(sources))
        bound.meta["business_source_instance"] = (source.name.lower(), id(scope), alias)
        bound.meta["business_nullable_side"] = _nullable_source(scope, alias)
        return bound
    if isinstance(source, Scope):
        key = (id(source), column.name.lower())
        if key not in trail:
            projection = _projection(source, column.name)
            if projection is not None:
                expanded = _expand(projection, source, trail | {key})
            elif isinstance(source.expression, exp.Select) and any(
                item.is_star for item in source.expression.selects
            ):
                expanded = _expand_column(exp.column(column.name), source, trail | {key})
            else:
                return exp.column(column.name.lower(), table="__unresolved__")
            if _nullable_source(scope, column.table.lower() if column.table else next(iter(sources))):
                for aggregate in expanded.find_all(exp.AggFunc):
                    aggregate.meta["business_projection_nullable"] = True
                for reference in expanded.find_all(exp.Column):
                    reference.meta["business_nullable_side"] = True
            return expanded
    return exp.column(column.name.lower(), table="__unresolved__")


def _expand(expression: exp.Expression, scope: Scope, trail: set[tuple[int, str]]) -> exp.Expression:
    if isinstance(expression, exp.Alias):
        expression = expression.this
    # Scalar queries and set operations need a separate semantic contract.
    if expression.find(exp.Query) is not None:
        return exp.column("unsupported_projection", table="__unresolved__")
    def resolve(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column):
            return _expand_column(node, scope, trail)
        if isinstance(node, exp.AggFunc):
            node.meta["business_aggregate_scope"] = id(scope)
            group = scope.expression.args.get("group")
            node.meta["business_nonempty_group"] = bool(
                group and group.expressions and not any(value for key, value in group.args.items() if key != "expressions")
            )
            node.meta["business_raw_tables"] = {
                source.name.lower() for source in _sources(scope).values() if isinstance(source, exp.Table)
            }
            node.meta["business_source_instances"] = {
                (source.name.lower(), id(scope), alias)
                for alias, source in _sources(scope).items() if isinstance(source, exp.Table)
            }
        if isinstance(node, exp.Count) and isinstance(node.this, exp.Star):
            node.meta["business_row_source"] = _row_source(scope)
        return node

    return expression.transform(resolve, copy=True)


def _row_source(scope: Scope, visited: set[int] | None = None) -> str:
    visited = visited or set()
    sources = _sources(scope)
    if id(scope) in visited or len(sources) != 1:
        return "__ambiguous_rows__"
    source = next(iter(sources.values()))
    if isinstance(source, exp.Table):
        return source.name.lower()
    query = source.expression
    if not isinstance(query, exp.Select) or query.args.get("group") or query.args.get("distinct"):
        return "__derived_rows__"
    if any(item.find(exp.AggFunc) is not None for item in query.selects):
        return "__derived_rows__"
    return _row_source(source, visited | {id(scope)})


def _active_scopes(root: Scope) -> list[Scope]:
    """Exclude unused CTEs: they cannot establish a contract for the actual query."""
    result: list[Scope] = []
    seen: set[int] = set()

    def visit(scope: Scope) -> None:
        if id(scope) in seen:
            return
        seen.add(id(scope))
        result.append(scope)
        for source in _sources(scope).values():
            if isinstance(source, Scope):
                visit(source)
        for child in (*scope.subquery_scopes, *scope.set_operation_scopes):
            visit(child)

    visit(root)
    return result


def _predicate_roots(scope: Scope) -> list[exp.Expression]:
    query = scope.expression
    if not isinstance(query, exp.Select):
        return []
    roots = [query.args.get("where"), query.args.get("having")]
    roots.extend(join.args.get("on") for join in query.args.get("joins", []))
    roots.extend(
        node.args.get("expression")
        for node in query.find_all(exp.Filter)
        if node.find_ancestor(exp.Select) is query
    )
    return [root for root in roots if isinstance(root, exp.Expression)]


def _direct_column(expression: exp.Expression, scope: Scope) -> tuple[str, str] | None:
    while isinstance(expression, (exp.Paren, exp.Cast, exp.TryCast)):
        expression = expression.this
    if not isinstance(expression, exp.Column):
        return None
    expanded = _expand_column(expression, scope, set())
    if isinstance(expanded, exp.Column) and expanded.table != "__unresolved__":
        return expanded.table.lower(), expanded.name.lower()
    return None


def _literal_value(expression: exp.Expression) -> Any:
    if isinstance(expression, exp.Literal):
        return expression.this if expression.is_string else Decimal(expression.this)
    if isinstance(expression, exp.Boolean):
        return expression.this
    return None


def _value_key(value: Any) -> tuple[str, Any]:
    if isinstance(value, bool):
        return "boolean", value
    if isinstance(value, (int, float, Decimal)):
        return "number", Decimal(str(value))
    return "string", str(value)


def _negated(expression: exp.Expression, stop: exp.Expression) -> bool:
    negated = False
    parent = expression.parent
    while parent is not None and parent is not stop:
        if isinstance(parent, exp.Not):
            negated = not negated
        parent = parent.parent
    return negated


def _comparison_key(predicate: exp.Expression, scope: Scope) -> tuple[str, str] | None:
    key = _direct_column(predicate.this, scope)
    if key is None and not isinstance(predicate, exp.In):
        key = _direct_column(predicate.expression, scope)
    return key


def _guarantees_selection(expression: exp.Expression, scope: Scope, key: tuple[str, str, str]) -> bool:
    if isinstance(expression, (exp.Paren, exp.Where, exp.Having, exp.Not)):
        return _guarantees_selection(expression.this, scope, key)
    if isinstance(expression, (exp.And, exp.Or)):
        left = _guarantees_selection(expression.this, scope, key)
        right = _guarantees_selection(expression.expression, scope, key)
        conjunction = isinstance(expression, exp.And) != _negated(expression, scope.expression)
        return (left or right) if conjunction else (left and right)
    if isinstance(expression, (exp.EQ, exp.NEQ, exp.In)):
        column = _comparison_key(expression, scope)
        excluded = isinstance(expression, exp.NEQ) != _negated(expression, scope.expression)
        return column is not None and (*column, "exclude" if excluded else "include") == key
    return False


def _check_schema(scopes: list[Scope], rewrite: dict[str, Any]) -> None:
    evidence = rewrite.get("source_evidence") or []
    evidenced_schema = next((item.get("schema") for item in evidence if isinstance(item, dict) and item.get("schema")), None)
    schema = str(rewrite.get("database_schema") or evidenced_schema or settings.pg_schema)
    tables = {str(item["table"]).lower() for item in [*rewrite.get("value_mappings", []), *rewrite.get("metrics", []), *rewrite.get("dimensions", [])]}
    tables.update(str(table).lower() for path in rewrite.get("join_paths", []) for table in path.get("tables", []))
    for scope in scopes:
        for table in _sources(scope).values():
            if not isinstance(table, exp.Table) or table.name.lower() not in tables or not table.db:
                continue
            identifier = table.args["db"]
            actual = table.db if identifier.args.get("quoted") else table.db.lower()
            if actual != schema:
                _reject(f"{table.name} 必须使用已核对的数据库模式 {schema}；不能改查其他模式中的同名表。")


def _check_mappings(scopes: list[Scope], mappings: list[dict[str, Any]]) -> None:
    planned: dict[tuple[str, str, str], set[tuple[str, Any]]] = {}
    for mapping in mappings:
        operator = str(mapping.get("operator") or "include")
        key = (str(mapping["table"]).lower(), str(mapping["column"]).lower(), operator)
        values = mapping["value"]
        values = values if isinstance(values, list) else [values]
        planned.setdefault(key, set()).update(_value_key(value) for value in values)
    used_tables = {
        source.name.lower()
        for scope in scopes
        for source in _sources(scope).values()
        if isinstance(source, exp.Table)
    }
    seen: dict[tuple[str, str, str], set[tuple[str, Any]]] = {}
    mapped_columns = {(table, column) for table, column, _ in planned}
    comparisons = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In)
    for scope in scopes:
        for root in _predicate_roots(scope):
            root_contracts: set[tuple[str, str, str]] = set()
            for predicate in root.find_all(*comparisons):
                if predicate.find_ancestor(exp.Select) is not scope.expression:
                    continue
                if isinstance(predicate, exp.In):
                    key = _direct_column(predicate.this, scope)
                    operands = predicate.expressions
                else:
                    key = _direct_column(predicate.this, scope)
                    operands = [predicate.expression]
                    if key is None:
                        key = _direct_column(predicate.expression, scope)
                        operands = [predicate.this]
                if key not in mapped_columns:
                    continue
                if not isinstance(predicate, (exp.EQ, exp.NEQ, exp.In)):
                    _reject(f"{key[0]}.{key[1]} 的枚举筛选必须使用等值、IN 或对应的排除条件。")
                excluded = isinstance(predicate, exp.NEQ) != _negated(predicate, scope.expression)
                contract_key = (*key, "exclude" if excluded else "include")
                if contract_key not in planned:
                    _reject(f"{key[0]}.{key[1]} 的包含或排除方向不符合问题，请保留已确认的筛选方向。")
                root_contracts.add(contract_key)
                values = {_value_key(value) for operand in operands if (value := _literal_value(operand)) is not None}
                if not values:
                    continue
                if not values.issubset(planned[contract_key]):
                    expected = "、".join(str(value) for _, value in sorted(planned[contract_key], key=str))
                    _reject(f"{key[0]}.{key[1]} 的筛选值不符合问题；应使用数据库中的值：{expected}。")
                seen.setdefault(contract_key, set()).update(values)
            for key in root_contracts:
                if not _guarantees_selection(root, scope, key):
                    _reject(f"{key[0]}.{key[1]} 的 OR 条件可能绕过已确认的筛选值，请将该筛选放在所有分支都必须满足的条件中。")
    for key, expected in planned.items():
        if key[0] in used_tables and seen.get(key, set()) != expected:
            _reject(f"查询遗漏了 {key[0]}.{key[1]} 已确认的业务筛选值，请完整应用问题中的筛选条件。")


def _zero_fill(expression: exp.Expression) -> bool:
    if not isinstance(expression, exp.Coalesce) or len(expression.expressions) != 1:
        return False
    default = expression.expressions[0]
    while isinstance(default, (exp.Paren, exp.Cast)):
        default = default.this
    return isinstance(default, exp.Literal) and not default.is_string and Decimal(default.this) == 0


def _zero_fill_operand(expression: exp.Expression) -> exp.Expression:
    while isinstance(expression, (exp.Paren, exp.Cast, exp.Round)) or _zero_fill(expression):
        expression = expression.this
    return expression


def _normal_form(expression: Any, preserve_precision: bool = False, null_sensitive: bool = False) -> Any:
    """Small, deliberately bounded equivalences for the configured metric expressions."""
    normal = lambda value: _normal_form(value, preserve_precision, null_sensitive)
    if not isinstance(expression, exp.Expression):
        if isinstance(expression, list):
            return tuple(normal(item) for item in expression)
        return expression
    if isinstance(expression, (exp.Alias, exp.Paren, exp.Filter)):
        return normal(expression.this)
    if _zero_fill(expression):
        value = _zero_fill_operand(expression.this)
        if isinstance(value, exp.Sum) or (
            isinstance(value, exp.Sub) and value.this.find(exp.Sum) and value.expression.find(exp.Sum)
        ) or (isinstance(value, exp.Avg) and value.meta.get("business_nonnull_value")) or (
            isinstance(value, exp.Count) and not value.meta.get("business_projection_nullable")
        ):
            return normal(expression.this)
    if not preserve_precision and isinstance(expression, (exp.Cast, exp.TryCast, exp.Round)) and expression.this.find(exp.AggFunc):
        return normal(expression.this)
    if isinstance(expression, exp.Column):
        return "column", expression.table.lower(), expression.name.lower()
    if isinstance(expression, exp.Literal):
        if expression.is_string:
            return "string", expression.this
        try:
            return "number", Decimal(expression.this)
        except InvalidOperation:
            return "number", expression.this
    if isinstance(expression, exp.Count):
        if isinstance(expression.this, exp.Star):
            return "count_star", expression.meta.get("business_row_source", "__unresolved__")
        return "count", normal(expression.this), normal(expression.expressions)
    if not null_sensitive and isinstance(expression, exp.Avg) and not isinstance(expression.this, exp.Distinct):
        tables = {column.table.lower() for column in expression.this.find_all(exp.Column)}
        source = next(iter(tables)) if len(tables) == 1 else "__unresolved__"
        return "div", normal(exp.Sum(this=expression.this.copy())), ("count_star", source)
    if not null_sensitive and isinstance(expression, exp.Sum) and isinstance(expression.this, (exp.Sub, exp.Add)):
        operand = expression.this
        return operand.key, normal(exp.Sum(this=operand.this.copy())), normal(exp.Sum(this=operand.expression.copy()))
    if isinstance(expression, exp.Nullif) and normal(expression.expression) == ("number", Decimal(0)):
        denominator = normal(expression.this)
        # AVG(net) and SUM(net)/NULLIF(COUNT(*), 0) have the same empty-group
        # result. A sales-amount divisor can be zero in a nonempty group, so
        # its NULLIF guard remains part of the metric contract.
        if isinstance(denominator, tuple) and denominator[0] == "count_star":
            return denominator
    if isinstance(expression, (exp.Sub, exp.Div)):
        return expression.key, normal(expression.this), normal(expression.expression)
    if isinstance(expression, (exp.Add, exp.Mul)):
        parts = [normal(expression.this), normal(expression.expression)]
        return expression.key, tuple(sorted(parts, key=repr))
    return expression.key, tuple(
        (name, normal(value))
        for name, value in sorted(expression.args.items())
        if value is not None and name not in {"alias", "comments"}
    )


def _expected_form(expression: str, table: str, preserve_precision: bool = False, null_sensitive: bool = False) -> Any:
    parsed = parse_one(expression, read="postgres")
    parsed = parsed.transform(
        lambda node: exp.column(node.name.lower(), table=(node.table or table).lower())
        if isinstance(node, exp.Column)
        else node,
    )
    for count in parsed.find_all(exp.Count):
        if isinstance(count.this, exp.Star):
            count.meta["business_row_source"] = table.lower()
    return _normal_form(parsed, preserve_precision, null_sensitive)


def _instance_columns(expression: exp.Expression) -> set[tuple[tuple, str]]:
    return {
        (column.meta["business_source_instance"], column.name.lower())
        for column in expression.find_all(exp.Column)
        if column.meta.get("business_source_instance")
    }


def _check_dimensions(root: Scope, dimensions: list[dict[str, Any]]) -> dict[str, set[tuple]]:
    observed: dict[str, set[tuple]] = {}
    if not isinstance(root.expression, exp.Select):
        if any(item.get("require_output") for item in dimensions):
            _reject("无法核对集合查询的业务维度输出，请在最外层 SELECT 明确投影维度列。")
        return observed
    outputs = [_expand(item, root, set()) for item in root.expression.selects]
    for dimension in dimensions:
        table, column = str(dimension["table"]).lower(), str(dimension["column"]).lower()
        instances: set[tuple] = set()
        for output in outputs:
            references = {(item.table.lower(), item.name.lower()) for item in output.find_all(exp.Column)}
            if references == {(table, column)} and output.find(exp.AggFunc, exp.Window) is None:
                instances.update(node for node, name in _instance_columns(output) if name == column)
        if dimension.get("require_output") and not instances:
            label = dimension.get("id") or column
            _reject(f"业务维度“{label}”的实际输出必须来自 {table}.{column}；不能用其他字段或仅在未输出的 CTE 中包含该列。")
        observed.setdefault(table, set()).update(instances)
    return observed


def _bound_instance(expression: exp.Expression, scope: Scope) -> tuple[tuple, str] | None:
    while isinstance(expression, (exp.Paren, exp.Cast, exp.TryCast)):
        expression = expression.this
    if not isinstance(expression, exp.Column):
        return None
    bound = _expand_column(expression, scope, set())
    if isinstance(bound, exp.Column) and bound.meta.get("business_source_instance"):
        return bound.meta["business_source_instance"], bound.name.lower()
    return None


def _relation_key(relation: dict[str, Any]) -> frozenset[tuple[str, str]]:
    return frozenset((
        (str(relation["left_table"]).lower(), str(relation["left_column"]).lower()),
        (str(relation["right_table"]).lower(), str(relation["right_column"]).lower()),
    ))


def _equality_pair(predicate: exp.Expression, scope: Scope) -> frozenset[tuple[tuple, str]] | None:
    if not isinstance(predicate, exp.EQ) or _negated(predicate, scope.expression):
        return None
    left, right = _bound_instance(predicate.this, scope), _bound_instance(predicate.expression, scope)
    return frozenset((left, right)) if left is not None and right is not None else None


def _guarantees_relation(expression: exp.Expression, scope: Scope, pair: frozenset) -> bool:
    if isinstance(expression, (exp.Paren, exp.Where, exp.Not)):
        return _guarantees_relation(expression.this, scope, pair)
    if isinstance(expression, (exp.And, exp.Or)):
        left = _guarantees_relation(expression.this, scope, pair)
        right = _guarantees_relation(expression.expression, scope, pair)
        conjunction = isinstance(expression, exp.And) != _negated(expression, scope.expression)
        return (left or right) if conjunction else (left and right)
    return _equality_pair(expression, scope) == pair


def _check_join_paths(root: Scope, scopes: list[Scope], rewrite: dict[str, Any], dimension_instances: dict[str, set[tuple]]) -> None:
    paths = rewrite.get("join_paths") or []
    if not paths:
        return
    expected = {_relation_key(relation) for path in paths for relation in path["joins"]}
    edges: dict[frozenset, set[frozenset[tuple]]] = {key: set() for key in expected}
    column_equalities: dict[tuple[tuple, str], set[tuple[tuple, str]]] = {}
    unique_keys = {
        (str(relation["right_table"]).lower(), str(relation["right_column"]).lower())
        for path in paths for relation in path["joins"]
        if relation.get("cardinality") in {"many_to_one", "one_to_one"}
    }
    bridges: dict[tuple, set[tuple]] = {}

    def is_bridge(pair: frozenset[tuple[tuple, str]]) -> bool:
        columns = {(node[0], column) for node, column in pair}
        return len(pair) == 2 and len(columns) == 1 and columns.issubset(unique_keys)

    def equivalent(nodes: set[tuple]) -> set[tuple]:
        # Only equality on a configured unique target key identifies the same
        # row across independent scans. Names and nonunique foreign keys do not.
        result = set(nodes)
        pending = list(nodes)
        while pending:
            node = pending.pop()
            for neighbor in bridges.get(node, set()) - result:
                result.add(neighbor)
                pending.append(neighbor)
        return result

    def add(pair: frozenset[tuple[tuple, str]] | None) -> None:
        if pair is None:
            return
        if len(pair) == 2:
            left_column, right_column = tuple(pair)
            column_equalities.setdefault(left_column, set()).add(right_column)
            column_equalities.setdefault(right_column, set()).add(left_column)
        key = frozenset((node[0], column) for node, column in pair)
        if key in edges:
            edges[key].add(frozenset(node for node, _ in pair))
        if is_bridge(pair):
            left, right = [node for node, _ in pair]
            bridges.setdefault(left, set()).add(right)
            bridges.setdefault(right, set()).add(left)

    for scope in scopes:
        if not isinstance(scope.expression, exp.Select):
            continue
        query = scope.expression
        predicates = [query.args.get("where"), *(join.args.get("on") for join in query.args.get("joins", []))]
        for predicate in predicates:
            if not isinstance(predicate, exp.Expression):
                continue
            for equality in predicate.find_all(exp.EQ):
                if equality.find_ancestor(exp.Select) is query:
                    pair = _equality_pair(equality, scope)
                    if pair is not None and _guarantees_relation(predicate, scope, pair):
                        add(pair)
        aliases = list(_sources(scope))
        for join in query.args.get("joins", []):
            right_alias = join.this.alias_or_name.lower()
            if right_alias not in aliases:
                continue
            left_aliases = aliases[:aliases.index(right_alias)]
            for identifier in join.args.get("using", []):
                right = _bound_instance(exp.column(identifier.name, table=right_alias), scope)
                candidates = []
                for alias in left_aliases:
                    left = _bound_instance(exp.column(identifier.name, table=alias), scope)
                    if left is not None and right is not None:
                        pair = frozenset((left, right))
                        if frozenset((node[0], column) for node, column in pair) in expected or is_bridge(pair):
                            candidates.append(pair)
                if len(candidates) == 1:
                    add(candidates[0])
                elif len(candidates) > 1:
                    _reject(f"USING ({identifier.name}) 的业务关联来源不唯一，请改用明确表别名的 ON 外键等值连接。")

    # A.id = B.foreign_id AND B.foreign_id = C.foreign_id entails
    # A.id = C.foreign_id. Preserve the concrete column/instance identities;
    # only configured FK endpoints and unique target keys create row edges.
    visited_columns: set[tuple[tuple, str]] = set()
    for column in list(column_equalities):
        if column in visited_columns:
            continue
        component, pending = set(), [column]
        while pending:
            current = pending.pop()
            if current not in component:
                component.add(current)
                pending.extend(column_equalities.get(current, set()) - component)
        visited_columns.update(component)
        members = list(component)
        for index, left in enumerate(members):
            for right in members[index + 1:]:
                pair = frozenset((left, right))
                key = frozenset((node[0], name) for node, name in pair)
                if key in expected or is_bridge(pair):
                    add(pair)

    actual_sources = {table: set(nodes) for table, nodes in dimension_instances.items()}
    metric_sources: dict[str, set[tuple]] = {}
    for metric in rewrite.get("metrics", []):
        projection = _projection(root, str(metric.get("output_alias") or ""))
        if projection is None:
            continue
        expanded = _expand(projection, root, set())
        table = str(metric["table"]).lower()
        _bind_count_sources(expanded, scopes, table, (rewrite.get("query_contract") or {}).get("schema_keys") or {})
        nodes = {node for node, _ in _instance_columns(expanded) if node[0] == table}
        for aggregate in expanded.find_all(exp.Count):
            if isinstance(aggregate.this, exp.Star):
                nodes.update(node for node in aggregate.meta.get("business_source_instances", set()) if node[0] == table)
        metric_sources.setdefault(table, set()).update(nodes)
    # Filter targets identify the actual table role even when a fixed value need
    # not be displayed (for example a single membership level's revenue).
    for mapping in rewrite.get("value_mappings", []):
        table, column = str(mapping["table"]).lower(), str(mapping["column"]).lower()
        for scope in scopes:
            for predicate in _predicate_roots(scope):
                for reference in predicate.find_all(exp.Column):
                    if reference.find_ancestor(exp.Select) is scope.expression:
                        bound = _bound_instance(reference, scope)
                        if bound is not None and bound[0][0] == table and bound[1] == column:
                            actual_sources.setdefault(table, set()).add(bound[0])
    all_instances = {
        (source.name.lower(), id(scope), alias)
        for scope in scopes for alias, source in _sources(scope).items() if isinstance(source, exp.Table)
    }
    for path in paths:
        tables = [str(table).lower() for table in path["tables"]]
        anchor, target = tables[0], tables[-1]
        frontier = equivalent(metric_sources.get(anchor) or actual_sources.get(anchor) or {node for node in all_instances if node[0] == anchor})
        for next_table, relation in zip(tables[1:], path["joins"]):
            following = set()
            for pair in edges[_relation_key(relation)]:
                if pair & frontier:
                    following.update(node for node in pair if node[0] == next_table)
            frontier = equivalent(following)
            if not frontier:
                _reject(f"业务关联路径缺少实际来源间的外键等值连接：{relation['left_table']}.{relation['left_column']} = {relation['right_table']}.{relation['right_column']}。请按已核对的路径关联，不要使用同名字段或其他城市归属替代。")
        targets = actual_sources.get(target) or {node for node in all_instances if node[0] == target}
        if not targets or not targets.issubset(frontier):
            _reject(f"业务维度或筛选的实际来源 {target} 未通过已配置关联路径连接到指标来源 {anchor}；额外的无关连接不能替代实际输出来源。")


def _metric_contexts(expression: exp.Expression, scope: Scope, seen: set[tuple[int, str]] | None = None):
    seen = seen or set()
    yield scope, expression
    sources = _sources(scope)
    for column in expression.find_all(exp.Column):
        alias = column.table.lower() if column.table else next(iter(sources)) if len(sources) == 1 else ""
        source = sources.get(alias)
        if isinstance(source, Scope) and (key := (id(source), column.name.lower())) not in seen:
            projection = _projection(source, column.name)
            if projection is not None:
                yield from _metric_contexts(projection, source, seen | {key})


def _source_tables(source: exp.Table | Scope, seen: set[int] | None = None) -> set[str]:
    if isinstance(source, exp.Table):
        return {source.name.lower()}
    seen = seen or set()
    if id(source) in seen:
        return set()
    return set().union(*(_source_tables(child, seen | {id(source)}) for child in _sources(source).values()))


def _unique_output_keys(scope: Scope, schema_keys: dict[str, Any] | None = None, seen: set[int] | None = None) -> list[set[str]]:
    seen = seen or set()
    schema_keys = schema_keys or {}
    query = scope.expression
    if id(scope) in seen or not isinstance(query, exp.Select):
        return []
    group = query.args.get("group")
    if group is not None:
        def identity(expression: exp.Expression):
            expanded = _expand(expression, scope, set())
            return _normal_form(expanded), frozenset(_instance_columns(expanded))

        names, grouped_columns = set(), {}
        for expression in group.expressions:
            if isinstance(expression, exp.Literal) and expression.is_int:
                position = int(expression.this) - 1
                if not 0 <= position < len(query.selects):
                    return []
                expression = query.selects[position]
            form = identity(expression)
            matches = [item.alias_or_name.lower() for item in query.selects if identity(item) == form]
            if not matches:
                return []
            names.add(matches[0])
            expanded = _expand(expression, scope, set())
            if isinstance(expanded, exp.Column) and expanded.meta.get("business_source_instance"):
                instance = expanded.meta["business_source_instance"]
                grouped_columns.setdefault(instance, {})[expanded.name.lower()] = matches[0]
        reduced = set(names)
        for instance, columns in grouped_columns.items():
            evidence = schema_keys.get(instance[0], {})
            primary = set(evidence.get("primary_key") or [])
            if evidence.get("metadata_verified") and primary and primary.issubset(columns):
                reduced.difference_update(name for column, name in columns.items() if column not in primary)
        return [names, reduced] if names else []
    if any(item.find(exp.AggFunc) is not None and item.find(exp.Window) is None for item in query.selects):
        return [set()]
    sources = _sources(scope)
    if len(sources) == 1:
        source = next(iter(sources.values()))
        if isinstance(source, Scope):
            keys = _unique_output_keys(source, schema_keys, seen | {id(scope)})
        else:
            evidence = schema_keys.get(source.name.lower(), {})
            keys = [set(key) for key in [evidence.get("primary_key") or [], *evidence.get("unique_keys", [])]
                    if key and evidence.get("metadata_verified")]
        result = []
        for key in keys:
            mapped = set()
            for name in key:
                matches = [item.alias_or_name.lower() for item in query.selects
                           if isinstance(value := item.this if isinstance(item, exp.Alias) else item, exp.Column)
                           and value.name.lower() == name]
                if not matches and any(item.is_star for item in query.selects):
                    matches = [name]
                if not matches:
                    break
                mapped.add(matches[0])
            else:
                result.append(mapped)
        return result
    return []


def _joined_keys(scope: Scope, alias: str, bound_aliases: set[str] | None = None) -> set[str]:
    query = scope.expression
    if not isinstance(query, exp.Select):
        return set()
    keys = set()
    roots = [query.args.get("where"), *(join.args.get("on") for join in query.args.get("joins", []))]
    for root in roots:
        if not isinstance(root, exp.Expression):
            continue
        for equality in root.find_all(exp.EQ):
            left, right = equality.this, equality.expression
            if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                continue
            if not left.table or not right.table or left.table.lower() == right.table.lower():
                continue
            if left.table.lower() not in _sources(scope) or right.table.lower() not in _sources(scope):
                continue
            pair = _equality_pair(equality, scope)
            if pair is None or not _guarantees_relation(root, scope, pair):
                continue
            for value, other in ((left, right), (right, left)):
                if value.table.lower() == alias and (bound_aliases is None or other.table.lower() in bound_aliases):
                    keys.add(value.name.lower())
    for join in query.args.get("joins", []):
        if join.this.alias_or_name.lower() == alias:
            aliases = list(_sources(scope))
            left = set(aliases[:aliases.index(alias)])
            if bound_aliases is None or (left and left.issubset(bound_aliases)):
                keys.update(item.name.lower() for item in join.args.get("using", []))
    return keys


def _source_unique_keys(source: exp.Table | Scope, schema_keys: dict[str, Any]) -> list[set[str]]:
    if isinstance(source, Scope):
        return _unique_output_keys(source, schema_keys)
    evidence = schema_keys.get(source.name.lower(), {})
    return [set(key) for key in [evidence.get("primary_key") or [], *evidence.get("unique_keys", [])]
            if key and evidence.get("metadata_verified")]


def _unproven_cardinality(scope: Scope, carriers: set[str], schema_keys: dict[str, Any]) -> set[str]:
    """Expand from fact carriers; disconnected unique-key cycles prove nothing."""
    sources = _sources(scope)
    bound = carriers & sources.keys()
    pending = set(sources) - bound
    if not bound:
        return pending
    while pending:
        following = {alias for alias in pending if any(
            key.issubset(_joined_keys(scope, alias, bound))
            for key in _source_unique_keys(sources[alias], schema_keys)
        )}
        if not following:
            break
        bound.update(following)
        pending.difference_update(following)
    return pending


def _count_row_carrier(scope: Scope, table: str, schema_keys: dict[str, Any], seen: set[int] | None = None) -> str | None:
    """Identify one nonnullable fact carrier, with every join proven at most one."""
    seen = seen or set()
    if id(scope) in seen or not isinstance(scope.expression, exp.Select):
        return None
    sources = _sources(scope)
    candidates = []
    for alias, source in sources.items():
        if isinstance(source, exp.Table):
            evidence = schema_keys.get(source.name.lower(), {})
            if source.name.lower() == table and evidence.get("metadata_verified") and evidence.get("primary_key"):
                candidates.append(alias)
        else:
            query = source.expression
            if (isinstance(query, exp.Select) and not query.args.get("group") and not query.args.get("distinct") and
                not any(item.find(exp.AggFunc) for item in query.selects) and
                _count_row_carrier(source, table, schema_keys, seen | {id(scope)}) is not None):
                candidates.append(alias)
    if len(candidates) != 1 or _nullable_source(scope, candidates[0]):
        return None
    return candidates[0] if not _unproven_cardinality(scope, {candidates[0]}, schema_keys) else None


def _count_origin_instances(
    scope: Scope, carrier: str, table: str, schema_keys: dict[str, Any], seen: set[int] | None = None,
) -> set[tuple]:
    seen = seen or set()
    if id(scope) in seen:
        return set()
    source = _sources(scope).get(carrier)
    if isinstance(source, exp.Table):
        return {(source.name.lower(), id(scope), carrier)} if source.name.lower() == table else set()
    if isinstance(source, Scope):
        inner = _count_row_carrier(source, table, schema_keys)
        if inner is None and _row_source(source) == table and len(_sources(source)) == 1:
            inner = next(iter(_sources(source)))
        if inner is not None:
            return _count_origin_instances(source, inner, table, schema_keys, seen | {id(scope)})
    return set()


def _bind_count_sources(expression: exp.Expression, scopes: list[Scope], table: str, schema_keys: dict[str, Any]) -> None:
    scope_map = {id(scope): scope for scope in scopes}
    for count in expression.find_all(exp.Count):
        if not isinstance(count.this, exp.Star):
            continue
        scope = scope_map.get(count.meta.get("business_aggregate_scope"))
        if scope is None:
            continue
        carrier = _count_row_carrier(scope, table, schema_keys)
        if carrier is None and count.meta.get("business_row_source") == table and len(_sources(scope)) == 1:
            carrier = next(iter(_sources(scope)))
        if carrier is not None:
            count.meta["business_row_source"] = table
            count.meta["business_count_carrier"] = carrier
            count.meta.setdefault("business_source_instances", set()).update(_count_origin_instances(scope, carrier, table, schema_keys))


def _check_projected_grain(
    projection: exp.Expression, root: Scope, unsafe: set[str], description: str,
    schema_keys: dict[str, Any] | None = None,
    metric_table: str | None = None,
) -> None:
    schema_keys = schema_keys or {}
    for scope, expression in _metric_contexts(projection, root):
        sources = _sources(scope)
        carriers = {column.table.lower() if column.table else next(iter(sources)) if len(sources) == 1 else ""
                    for column in expression.find_all(exp.Column)}
        if metric_table and any(isinstance(count.this, exp.Star) for count in expression.find_all(exp.Count)):
            carrier = _count_row_carrier(scope, metric_table, schema_keys)
            if carrier is not None:
                carriers.add(carrier)
        if not carriers and len(sources) == 1:
            carriers = set(sources)
        for alias in sorted(_unproven_cardinality(scope, carriers, schema_keys)):
            source = sources[alias]
            tables = _source_tables(source)
            verified = any(schema_keys.get(table, {}).get("metadata_verified") for table in tables)
            if not (tables & unsafe or verified):
                continue
            _reject(f"指标“{description}”关联的 {alias} 尚不能从指标来源证明按连接键至多一行，会重复展示或累计金额/评价；请按完整唯一键连接到已确认来源，或先分别聚合再关联。",
                    "join_fanout", {"source_aliases": [alias]})


def _check_metrics(
    root: Scope, metrics: list[dict[str, Any]], unsafe_tables: list[str], schema_keys: dict[str, Any] | None = None,
    nullable_metric_ids: set[str] | None = None,
    nonnull_columns: set[tuple[str, str]] | None = None,
) -> None:
    schema_keys = schema_keys or {}
    nullable_metric_ids = nullable_metric_ids or set()
    nonnull_columns = nonnull_columns or set()
    scopes = _active_scopes(root)
    for metric in metrics:
        alias = str(metric.get("output_alias") or "")
        projection = _projection(root, alias) if alias else None
        description = str(metric.get("description") or metric.get("id") or alias)
        details = {"metric_id": str(metric["id"])[:128], "output_alias": alias[:128], "source_table": str(metric["table"])[:128]}
        if projection is None:
            _reject(f"指标“{description}”必须使用输出别名 {alias or metric.get('id', 'metric')}，以便核对业务口径。")
        expanded = _mark_nonnull_averages(_expand(projection, root, set()), nonnull_columns, scopes, schema_keys)
        table = str(metric["table"]).lower()
        _bind_count_sources(expanded, scopes, table, schema_keys)
        unsafe = {str(table).lower() for table in metric.get("unsafe_join_tables", unsafe_tables)}
        for aggregate in expanded.find_all(exp.AggFunc):
            if isinstance(aggregate, exp.Count) and isinstance(aggregate.this, exp.Distinct):
                continue
            raw = aggregate.meta.get("business_raw_tables", set())
            joined = {table for table in raw & unsafe if not schema_keys.get(table, {}).get("metadata_verified")}
            if str(metric["table"]).lower() in raw and joined:
                _reject(f"指标“{description}”直接关联了可能产生多条明细的表（{'、'.join(sorted(joined))}），会重复计算；请先按指标来源粒度独立聚合后再关联。",
                        "join_fanout", {**details, "source_aliases": sorted(joined)})
        null_sensitive = str(metric["id"]) in nullable_metric_ids
        actual = _normal_form(expanded, null_sensitive=null_sensitive)
        expected = _metric_forms(metric, null_sensitive=null_sensitive)
        if actual not in expected:
            unbound = [node for node in expanded.find_all(exp.Count)
                       if isinstance(node.this, exp.Star) and node.meta.get("business_row_source") != table]
            if unbound:
                aliases = sorted({node[2] for count in unbound for node in count.meta.get("business_source_instances", set())})
                _reject(f"指标“{description}”（{alias}）的 COUNT(*) 尚不能证明是一条 {table} 记录计一次；请使用事实表的非空主键计数，并先处理一对多关联。事实来源位于可空关联侧或缺少完整唯一键证据时不能直接计全部连接行。",
                        "count_source_ambiguity", {**details, "source_aliases": aliases})
            if any(_zero_fill(node) and isinstance(value := _zero_fill_operand(node.this), exp.Avg) and
                   not value.meta.get("business_nonnull_value") for node in expanded.find_all(exp.Coalesce)):
                _reject(f"指标“{description}”（{alias}）的 AVG 补零尚不能证明与原均值等价；字段可能为空、聚合输入可能为空或投影经过可空关联。请保留原 AVG，或依据明确的非空及非 NULL 证据生成。",
                        "nullable_aggregate_zero_fill", details)
            _reject(f"指标“{description}”（{alias}）的实际输出计算不符合业务口径，请使用已确认的指标表达式，并将列限定到真实表或其别名。",
                    "metric_formula", details)
        try:
            _check_projected_grain(projection, root, unsafe, description, schema_keys, table)
        except AppError as error:
            if getattr(error, "business_failure_reason", None) == "join_fanout":
                error.business_failure_details.update(details)
            raise


def _resolve_projection(expression: exp.Expression, scope: Scope, seen: set[tuple[int, str]] | None = None):
    """Follow a single projected value; never use an unrelated window in the tree."""
    seen = seen or set()
    while isinstance(expression, (exp.Alias, exp.Paren, exp.Cast, exp.TryCast)):
        expression = expression.this
    if not isinstance(expression, exp.Column):
        return expression, scope
    sources = _sources(scope)
    alias = expression.table.lower() if expression.table else next(iter(sources)) if len(sources) == 1 else ""
    source = sources.get(alias)
    key = (id(source), expression.name.lower())
    if not isinstance(source, Scope) or key in seen:
        return expression, scope
    projected = _projection(source, expression.name)
    if projected is None and any(item.is_star for item in source.expression.selects):
        projected = exp.column(expression.name)
    return _resolve_projection(projected, source, seen | {key}) if projected is not None else (expression, scope)


def _metric_forms(metric: dict[str, Any], preserve_precision: bool = False, null_sensitive: bool = False) -> list[Any]:
    return [_expected_form(str(value), str(metric["table"]), preserve_precision, null_sensitive)
            for value in [metric["expression"], *metric.get("alternatives", [])]]


def _nullable_metric_ids(rewrite: dict[str, Any]) -> set[str]:
    return {str(item["id"]) for item in (rewrite.get("query_contract") or {}).get("metrics", [])
            if any(str(value).upper() == "YES" for value in item.get("source", {}).get("nullability", {}).values())}


def _nonnull_columns(rewrite: dict[str, Any]) -> set[tuple[str, str]]:
    return {tuple(key.lower().rsplit(".", 1))
            for metric in (rewrite.get("query_contract") or {}).get("metrics", [])
            for key, nullable in metric.get("source", {}).get("nullability", {}).items()
            if str(nullable).upper() == "NO" and "." in key}


def _positive_count_gate(scope: Scope, instance: tuple, primary_key: set[str]) -> bool:
    """A positive count of a real PK proves this source contributes a row."""
    def guarantees(predicate: exp.Expression) -> bool:
        if isinstance(predicate, (exp.Where, exp.Having, exp.Paren)):
            return guarantees(predicate.this)
        if isinstance(predicate, (exp.And, exp.Or)):
            left, right = guarantees(predicate.this), guarantees(predicate.expression)
            return left or right if isinstance(predicate, exp.And) else left and right
        if not isinstance(predicate, (exp.GTE, exp.GT, exp.LTE, exp.LT)):
            return False
        value, threshold = predicate.this, _literal_value(predicate.expression)
        forward = isinstance(predicate, (exp.GTE, exp.GT))
        if not isinstance(threshold, Decimal):
            value, threshold = predicate.expression, _literal_value(predicate.this)
            forward = isinstance(predicate, (exp.LTE, exp.LT))
        if not forward or not isinstance(threshold, Decimal):
            return False
        if threshold < 0 or (threshold == 0 and isinstance(predicate, (exp.GTE, exp.LTE))):
            return False
        expanded = _expand(value, scope, set())
        return (isinstance(expanded, exp.Count) and isinstance(expanded.this, exp.Column) and
                expanded.this.meta.get("business_source_instance") == instance and
                expanded.this.name.lower() in primary_key)

    return any(guarantees(predicate) for predicate in
               [scope.expression.args.get("where"), scope.expression.args.get("having")]
               if isinstance(predicate, exp.Expression))


def _mark_nonnull_averages(
    expression: exp.Expression, nonnull: set[tuple[str, str]], scopes: list[Scope], schema_keys: dict[str, Any],
) -> exp.Expression:
    """Only a direct NN field with nonempty input can make AVG zero-fill redundant."""
    scope_map = {id(scope): scope for scope in scopes}
    for average in expression.find_all(exp.Avg):
        value = average.this
        if (not isinstance(value, exp.Column) or (value.table.lower(), value.name.lower()) not in nonnull or
            average.meta.get("business_projection_nullable")):
            continue
        nonempty = average.meta.get("business_nonempty_group") and not value.meta.get("business_nullable_side")
        instance = value.meta.get("business_source_instance")
        evidence = schema_keys.get(value.table.lower(), {})
        primary = set(evidence.get("primary_key") or [])
        scope = scope_map.get(average.meta.get("business_aggregate_scope"))
        if not nonempty and scope is not None and instance and evidence.get("metadata_verified") and primary:
            nonempty = _positive_count_gate(scope, instance, primary)
        if nonempty:
            average.meta["business_nonnull_value"] = True
    return expression


def _lineage_instances(expression: exp.Expression) -> set[tuple]:
    result = {node for node, _ in _instance_columns(expression)}
    for aggregate in expression.find_all(exp.AggFunc):
        result.update(aggregate.meta.get("business_source_instances", set()))
    return result


def _nullable_source(scope: Scope, alias: str) -> bool:
    aliases = list(_sources(scope))
    for join in scope.expression.args.get("joins", []):
        right = join.this.alias_or_name.lower()
        side = str(join.args.get("side") or "").upper()
        if side == "FULL" or (side == "LEFT" and right == alias):
            return True
        if side == "RIGHT" and right in aliases and alias in aliases[:aliases.index(right)]:
            return True
    return False


def _minimum_before_window(scope: Scope, metric: dict[str, Any], minimum: int, schema_keys: dict[str, Any] | None = None) -> bool:
    """Prove a configured count gate along its nonnullable projection lineage."""
    output = _projection(scope, str(metric["output_alias"])) or exp.column(str(metric["output_alias"]))
    forms = _metric_forms(metric, preserve_precision=True)
    schema_keys = schema_keys or {}
    scopes = _active_scopes(scope)
    expanded_output = _expand(output, scope, set())
    _bind_count_sources(expanded_output, scopes, str(metric["table"]).lower(), schema_keys)
    origin = _lineage_instances(expanded_output)

    def guarantees(predicate: exp.Expression, current: Scope) -> bool:
        if isinstance(predicate, (exp.Where, exp.Having, exp.Paren)):
            return guarantees(predicate.this, current)
        if isinstance(predicate, (exp.And, exp.Or)):
            left, right = guarantees(predicate.this, current), guarantees(predicate.expression, current)
            return left or right if isinstance(predicate, exp.And) else left and right
        if not isinstance(predicate, (exp.GTE, exp.GT, exp.LTE, exp.LT)):
            return False
        value, threshold = predicate.this, _literal_value(predicate.expression)
        forward = isinstance(predicate, (exp.GTE, exp.GT))
        if not isinstance(threshold, Decimal):
            value, threshold = predicate.expression, _literal_value(predicate.this)
            forward = isinstance(predicate, (exp.LTE, exp.LT))
        strict = isinstance(predicate, (exp.GT, exp.LT))
        if not forward or threshold != Decimal(minimum - (1 if strict else 0)):
            return False
        expanded = _expand(value, current, set())
        _bind_count_sources(expanded, scopes, str(metric["table"]).lower(), schema_keys)
        return _normal_form(expanded, True) in forms and _lineage_instances(expanded) == origin

    def visit(current: Scope, expression: exp.Expression, seen: set[tuple[int, str]]) -> bool:
        if any(guarantees(predicate, current) for predicate in
               [current.expression.args.get("where"), current.expression.args.get("having")]
               if isinstance(predicate, exp.Expression)):
            return True
        while isinstance(expression, (exp.Alias, exp.Paren, exp.Cast, exp.TryCast, exp.Round)):
            expression = expression.this
        if not isinstance(expression, exp.Column):
            return False
        sources = _sources(current)
        alias = expression.table.lower() if expression.table else next(iter(sources)) if len(sources) == 1 else ""
        source = sources.get(alias)
        key = (id(source), expression.name.lower())
        if not isinstance(source, Scope) or key in seen or _nullable_source(current, alias):
            return False
        projected = _projection(source, expression.name)
        if projected is None and any(item.is_star for item in source.expression.selects):
            projected = exp.column(expression.name)
        return projected is not None and visit(source, projected, seen | {key})

    return bool(origin) and visit(scope, output, set())


def _key_provenance_matches(expression: exp.Expression, scope: Scope, key: str, scopes: list[Scope]) -> bool:
    bound = _bound_instance(expression, scope)
    if bound is None:
        return False
    expected = tuple(key.lower().rsplit(".", 1))
    equalities: dict[tuple, set[tuple]] = {}
    for current in scopes:
        for predicate in _predicate_roots(current):
            for equality in predicate.find_all(exp.EQ):
                pair = _equality_pair(equality, current)
                if pair is not None and len(pair) == 2 and _guarantees_relation(predicate, current, pair):
                    left, right = tuple(pair)
                    equalities.setdefault(left, set()).add(right)
                    equalities.setdefault(right, set()).add(left)
    pending, visited = [bound], set()
    while pending:
        node = pending.pop()
        if (node[0][0], node[1]) == expected:
            return True
        if node not in visited:
            visited.add(node)
            pending.extend(equalities.get(node, set()) - visited)
    return False


def _post_rank_filter(root: Scope, projection: exp.Expression, window: exp.Window, window_scope: Scope, condition: dict[str, Any]) -> bool:
    """Prove a declared comparison after this actual output window is computed."""
    operators = {exp.LTE: "lte", exp.LT: "lt", exp.GTE: "gte", exp.GT: "gt"}
    reversed_operators = {"lte": "gte", "lt": "gt", "gte": "lte", "gt": "lt"}
    required, threshold = condition["operator"], Decimal(str(condition["maximum"]))

    def guarantees(predicate: exp.Expression, scope: Scope) -> bool:
        if isinstance(predicate, (exp.Where, exp.Having, exp.Paren)):
            return guarantees(predicate.this, scope)
        if isinstance(predicate, (exp.And, exp.Or)):
            left, right = guarantees(predicate.this, scope), guarantees(predicate.expression, scope)
            return left or right if isinstance(predicate, exp.And) else left and right
        operator = operators.get(type(predicate))
        if operator is None:
            return False
        value, actual = predicate.this, _literal_value(predicate.expression)
        if not isinstance(actual, Decimal):
            value, actual = predicate.expression, _literal_value(predicate.this)
            operator = reversed_operators[operator]
        if not isinstance(actual, Decimal):
            return False
        if (operator, actual) != (required, threshold):
            equivalents = {"lte": ("lt", threshold + 1), "gt": ("gte", threshold + 1),
                           "lt": ("lte", threshold - 1), "gte": ("gt", threshold - 1)}
            if (operator, actual) != equivalents.get(required):
                return False
        resolved, resolved_scope = _resolve_projection(value, scope)
        return resolved is window and resolved_scope is window_scope

    def visit(scope: Scope, expression: exp.Expression, seen: set[tuple[int, str]]) -> bool:
        if scope is window_scope:
            return False
        if any(guarantees(predicate, scope) for predicate in
               [scope.expression.args.get("where"), scope.expression.args.get("having")]
               if isinstance(predicate, exp.Expression)):
            return True
        while isinstance(expression, (exp.Alias, exp.Paren, exp.Cast, exp.TryCast)):
            expression = expression.this
        if not isinstance(expression, exp.Column):
            return False
        sources = _sources(scope)
        alias = expression.table.lower() if expression.table else next(iter(sources)) if len(sources) == 1 else ""
        source = sources.get(alias)
        key = (id(source), expression.name.lower())
        if not isinstance(source, Scope) or key in seen or _nullable_source(scope, alias):
            return False
        projected = _projection(source, expression.name)
        if projected is None and any(item.is_star for item in source.expression.selects):
            projected = exp.column(expression.name)
        return projected is not None and visit(source, projected, seen | {key})

    return visit(root, projection, set())


def _check_rank_contracts(root: Scope, rewrite: dict[str, Any]) -> None:
    """Check declared parallel windows, not arbitrary natural-language ranking intent."""
    metrics = {str(item["id"]): item for item in rewrite.get("metrics", [])}
    nullable_metric_ids = _nullable_metric_ids(rewrite)
    nonnull, scopes = _nonnull_columns(rewrite), _active_scopes(root)
    schema_keys = (rewrite.get("query_contract") or {}).get("schema_keys") or {}
    for contract in (rewrite.get("query_contract") or {}).get("analysis", []):
        if contract.get("kind") != "parallel_rankings":
            continue
        windows, resolved_windows = [], {}
        for ranking in contract.get("rankings", []):
            alias = str(ranking["output_alias"])
            projection = _projection(root, alias)
            if projection is None:
                _reject(f"结构化排名必须实际输出 {alias}，以便核对排序口径。")
            window, scope = _resolve_projection(projection, root)
            if not isinstance(window, exp.Window) or window.this.sql_name() != contract["policy"]:
                _reject(f"排名 {alias} 必须使用已配置的 {contract['policy']} 窗口函数。")
            order = window.args.get("order")
            ordered = order.expressions if isinstance(order, exp.Order) else []
            metric = metrics.get(str(ranking["metric_id"]))
            if not ordered or metric is None:
                _reject(f"无法核对排名 {alias} 的业务指标排序。")
            ordered_metric = _mark_nonnull_averages(_expand(ordered[0].this, scope, set()), nonnull, scopes, schema_keys)
            output_metric = _mark_nonnull_averages(_expand(_projection(root, str(metric["output_alias"])), root, set()), nonnull, scopes, schema_keys)
            _bind_count_sources(ordered_metric, scopes, str(metric["table"]).lower(), schema_keys)
            _bind_count_sources(output_metric, scopes, str(metric["table"]).lower(), schema_keys)
            null_sensitive = str(metric["id"]) in nullable_metric_ids
            actual = _normal_form(ordered_metric, True, null_sensitive)
            if (actual not in _metric_forms(metric, True, null_sensitive) or
                _lineage_instances(ordered_metric) != _lineage_instances(output_metric) or
                bool(ordered[0].args.get("desc")) != (ranking.get("direction", "desc") == "desc")):
                _reject(f"排名 {alias} 必须按已确认的指标 {metric['output_alias']} 及方向排序；排名前不能舍入或替换评分来源。",
                        "rank_metric_mismatch", {"metric_id": str(metric["id"]), "output_alias": alias, "source_table": str(metric["table"])})
            upstream = _active_scopes(scope)
            partitions, configured = window.args.get("partition_by") or [], ranking.get("partition_by") or []
            if len(partitions) != len(configured) or any(not _key_provenance_matches(value, scope, key, upstream) for value, key in zip(partitions, configured)):
                _reject(f"排名 {alias} 的分区不符合已配置的候选集合。", "rank_population", {"output_alias": alias})
            ties = contract.get("tie_keys") or []
            if len(ordered) != 1 + len(ties) or any(
                bool(value.args.get("desc")) or not _key_provenance_matches(value.this, scope, key, upstream)
                for value, key in zip(ordered[1:], ties)
            ):
                _reject(f"排名 {alias} 必须按配置的实体键稳定排序：{'、'.join(ties)}。", "rank_population", {"output_alias": alias, "tie_keys": ties})
            if any(current.expression.args.get("limit") or current.expression.args.get("offset") or
                   any(window.find_ancestor(exp.Select) is current.expression for window in current.expression.find_all(exp.Window))
                   for current in upstream if current is not scope):
                _reject("配置要求先形成完整候选集合再并列排名；不能在输入层先截取数量或按另一项排名筛选。", "rank_population", {"output_alias": alias})
            windows.append(scope)
            resolved_windows[alias] = (projection, window, scope)
        if contract.get("shared_population") and len({id(scope) for scope in windows}) != 1:
            _reject("多个排名必须在同一候选层计算，再在外层筛选排名结果；不能先筛一项排名再计算另一项。", "rank_population")
        minimum = contract.get("population_min_count")
        if minimum and (minimum.get("operator") != "gte" or not windows or not _minimum_before_window(
            windows[0], metrics[str(minimum["metric_id"])], int(minimum["minimum"]), schema_keys
        )):
            _reject(f"请在所有排名计算前，对同一候选集合应用 {minimum['output_alias']} >= {minimum['minimum']} 的数量门槛；外层排名后筛选或可空关联内的门槛不能替代。", "rank_population", {"metric_id": str(minimum["metric_id"]), "output_alias": str(minimum["output_alias"])})
        for condition in contract.get("post_rank_filters", []):
            alias = str(condition["output_alias"])
            resolved = resolved_windows.get(alias)
            if resolved is None or not _post_rank_filter(root, *resolved, condition):
                _reject(f"排名计算后必须对实际输出 {alias} 应用 {condition['operator']} {condition['maximum']} 的条件；不能反向筛选、遗漏条件或通过 OR 分支绕过。", "rank_post_filter", {"output_alias": alias})


def validate_business_sql(sql: str, rewrite: dict[str, Any]) -> None:
    """Validate explicit rewrite contracts after the separate SQL safety validation."""
    mappings = rewrite.get("value_mappings") or []
    metrics = rewrite.get("metrics") or []
    dimensions = rewrite.get("dimensions") or []
    rankings = any(item.get("kind") == "parallel_rankings" for item in (rewrite.get("query_contract") or {}).get("analysis", []))
    if not mappings and not metrics and not dimensions and not rewrite.get("join_paths") and not rankings:
        return
    try:
        root = build_scope(parse_one(sql, read="postgres"))
        if root is None:
            _reject("无法核对查询的业务口径，请生成完整的 SELECT 查询。")
        scopes = _active_scopes(root)
        _check_schema(scopes, rewrite)
        _check_mappings(scopes, mappings)
        _check_metrics(root, metrics, rewrite.get("unsafe_join_tables") or [], (rewrite.get("query_contract") or {}).get("schema_keys"), _nullable_metric_ids(rewrite), _nonnull_columns(rewrite))
        instances = _check_dimensions(root, dimensions)
        _check_join_paths(root, scopes, rewrite, instances)
        _check_rank_contracts(root, rewrite)
    except AppError:
        raise
    except Exception:
        _reject("无法确定查询是否符合已确认的业务口径，请简化投影表达式并保留指定输出别名。")


def business_result_dimensions(
    sql: str, rewrite: dict[str, Any], columns: list[str]
) -> dict[str, str]:
    """Map configured dimension IDs to uniquely identified result column names.

    This reads projection provenance only; it does not infer ranking semantics or
    change SQL. Ambiguous matches, wildcard/set-operation projections and invalid
    inputs are omitted, so callers must not guess a missing dimension's column.
    """
    try:
        root = build_scope(parse_one(sql, read="postgres"))
        if root is None or not isinstance(root.expression, exp.Select):
            return {}
        projections = root.expression.selects
        if len(projections) != len(columns) or any(item.is_star for item in projections):
            return {}
        outputs = [_expand(item, root, set()) for item in projections]
        candidates: dict[str, set[str]] = {}
        ambiguous_ids: set[str] = set()
        for dimension in rewrite.get("dimensions") or []:
            dimension_id = str(dimension.get("id") or dimension["column"])
            expected = (str(dimension["table"]).lower(), str(dimension["column"]).lower())
            matches = [
                index for index, output in enumerate(outputs)
                if {(column.table.lower(), column.name.lower()) for column in output.find_all(exp.Column)} == {expected}
                and output.find(exp.AggFunc, exp.Window) is None
            ]
            if len(matches) != 1 or columns.count(columns[matches[0]]) != 1:
                ambiguous_ids.add(dimension_id)
                continue
            candidates.setdefault(dimension_id, set()).add(columns[matches[0]])
        return {
            dimension_id: next(iter(names)) for dimension_id, names in candidates.items()
            if len(names) == 1 and dimension_id not in ambiguous_ids
        }
    except Exception:
        return {}
