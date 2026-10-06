"""Choose a small, declarative chart locally from SQL evidence and returned data."""

import math
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import OptimizeError, ParseError, TokenError
from sqlglot.optimizer.scope import Scope, build_scope

from app.core.errors import AppError
from app.services.chart_service import validate_chart_config


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (int, float, Decimal, str)):
        return None
    if isinstance(value, str) and not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value.strip()):
        return None
    try:
        number = Decimal(str(value).strip())
        return number if number.is_finite() and math.isfinite(float(number)) else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}(?:$|-\d{2})", value):
        try:
            return _date(datetime.fromisoformat(value + "-01" if len(value) == 7 else value.replace("Z", "+00:00")))
        except ValueError:
            pass
    return None


def _expanded(node: exp.Expression, scope: Scope, depth: int = 0) -> exp.Expression:
    """Follow projected columns just enough to identify metric and time roles."""
    if depth >= 12:
        return node.copy()
    sources = {name: source for name, (_, source) in scope.selected_sources.items()}

    def resolve(column: exp.Expression) -> exp.Expression:
        if not isinstance(column, exp.Column):
            return column
        source = sources.get(column.table) if column.table else next(iter(sources.values())) if len(sources) == 1 else None
        if isinstance(source, Scope):
            matches = [item for item in source.expression.selects if item.alias_or_name == column.name]
            if len(matches) == 1:
                return _expanded(matches[0].this if isinstance(matches[0], exp.Alias) else matches[0], source, depth + 1)
        if isinstance(source, exp.Table):
            column.meta["chart_source_table"] = source.name
            column.meta["chart_source_column"] = column.name
        return column

    return (node.this if isinstance(node, exp.Alias) else node).transform(resolve, copy=True)


def _sql_info(sql: str, columns: list[str]) -> dict[str, Any]:
    query = parse_one(sql, read="postgres")
    scope = build_scope(query)
    if scope is None or not isinstance(query, exp.Select):
        return {"query": query, "expressions": {}, "order_fields": []}
    projections = query.selects
    if len(projections) == 1 and projections[0].is_star and len(scope.selected_sources) == 1:
        source = next(iter(scope.selected_sources.values()))[1]
        if isinstance(source, Scope):
            projections, scope = source.expression.selects, source
    expressions = {}
    if len(projections) == len(columns) and not any(item.is_star for item in projections):
        expressions = {field: _expanded(projection, scope) for field, projection in zip(columns, projections)}
    order = query.args.get("order")
    return {"query": query, "expressions": expressions,
            "order_fields": [item.this.name for item in order.expressions if isinstance(item.this, exp.Column)] if order else []}


def _unit(field: str, expression: exp.Expression | None, metric: dict) -> str | None:
    # An alias such as ratio/share does not establish a percentage scale.
    # Display units require configured evidence; values are never converted.
    return str(metric["unit"]) if metric.get("unit") else None


def _time_grain(expression: exp.Expression | None) -> str | None:
    while isinstance(expression, (exp.Cast, exp.TryCast, exp.Paren)):
        expression = expression.this
    if expression is None or expression.key not in {"timestamptrunc", "datetrunc", "extract"}:
        return None
    unit = expression.args.get("unit") or expression.args.get("this")
    name = str(unit.name if isinstance(unit, exp.Expression) else unit).lower()
    return name if name in {"year", "quarter", "month", "week", "day", "hour", "minute", "second"} else None


def _profiles(columns: list[str], rows: list[list[Any]], info: dict, rewrite: dict) -> list[dict]:
    contract = rewrite.get("query_contract") or {}
    entity_keys = {key for entity in contract.get("entities", []) for key in entity.get("entity_keys", [])}
    entity_keys.update(f"{table}.{column}" for table, keys in contract.get("schema_keys", {}).items() for column in keys.get("primary_key", []))
    metrics = {item.get("output_alias"): item for item in rewrite.get("metrics", [])}
    for item in (rewrite.get("query_contract") or {}).get("metrics", []):
        metrics[item.get("output_alias")] = {**metrics.get(item.get("output_alias"), {}), **item}
    profiles = []
    for index, field in enumerate(columns):
        values = [row[index] for row in rows]
        present = [value for value in values if value is not None]
        numbers, dates = [_number(value) for value in present], [_date(value) for value in present]
        expression = info["expressions"].get(field)
        source_columns = list(dict.fromkeys(f"{node.meta['chart_source_table']}.{node.meta['chart_source_column']}"
                                            for node in expression.find_all(exp.Column) if node.meta.get("chart_source_table") and node.meta.get("chart_source_column"))) if expression is not None else []
        metric = metrics.get(field, {})
        grain = _time_grain(expression)
        kind = ("empty" if not present else "boolean" if all(isinstance(value, bool) for value in present)
                else "date" if all(value is not None for value in dates)
                else "numeric" if all(value is not None for value in numbers)
                else "category" if all(isinstance(value, str) for value in present) else "mixed")
        role = "metric" if metric or expression is not None and expression.find(exp.AggFunc) else "category"
        if expression is not None and expression.find(exp.Window) is not None or re.search(r"(?:^|_)(?:rank|ranking|position)(?:_|$)|排名|名次", field, re.I):
            role = "rank"
        elif re.search(r"(?:^|_)id$|编号$", field, re.I) or isinstance(expression, exp.Column) and (
                re.search(r"(?:^|_)id$", expression.name, re.I) or kind == "numeric" and any(key in entity_keys for key in source_columns)):
            role = "id"
        elif grain or kind == "date" and (any(isinstance(value, (date, datetime)) for value in present) or
                re.search(r"date|time|month|year|period|_at\b|日期|时间|月份", field + " " + (expression.sql() if expression is not None else ""), re.I)):
            role = "time"
        elif kind == "numeric" and role == "category":
            role = "metric"
        additive = metric.get("additive")
        if additive is None and expression is not None:
            additive = bool(isinstance(expression, (exp.Sum, exp.Count)) and expression.find(exp.Distinct) is None and
                            not any(isinstance(child, (exp.AggFunc, exp.Window)) for child in expression.this.walk()))
        if expression is not None and (expression.find(exp.Distinct) is not None or isinstance(expression, exp.Avg)):
            additive = False
        # A month/day number alone is a repeating calendar component, not an
        # absolute timeline. YEAR and returned ISO/native dates are ordered.
        ordered = dates if role == "time" and kind == "date" else numbers if role == "time" and grain == "year" and kind == "numeric" and all(1 <= value <= 9999 and value == value.to_integral() for value in numbers) else []
        direction = None
        if ordered and len(ordered) == len(values) and len(set(ordered)) == len(ordered):
            direction = "asc" if ordered == sorted(ordered) else "desc" if ordered == sorted(ordered, reverse=True) else None
        profiles.append({"name": field, "kind": kind, "role": role, "unit": _unit(field, expression, metric),
                         "display_name": str(metric.get("display_name") or field)[:100], "metric_id": metric.get("id"),
                         "source_columns": source_columns,
                         "additive": additive is True, "non_null_count": len(present), "null_count": len(values) - len(present),
                         "unique_count": len(set(numbers)) if kind == "numeric" else len(set(dates)) if kind == "date" else len({str(value) for value in present}), "time_order": direction, "time_grain": grain,
                         "min": str(min(numbers)) if kind == "numeric" else None,
                         "max": str(max(numbers)) if kind == "numeric" else None})
    return profiles


def _primary(profiles: list[dict], question: str, info: dict, rewrite: dict) -> dict | None:
    numeric = [item for item in profiles if item["kind"] == "numeric" and item["role"] == "metric"]
    if not numeric:
        return None
    by_id = {item.get("metric_id"): item for item in numeric if item.get("metric_id")}
    interpretations = rewrite.get("interpretations") or {}
    interpretation_rules = list(interpretations.values()) if isinstance(interpretations, dict) else interpretations if isinstance(interpretations, list) else []
    rules = [*interpretation_rules, *rewrite.get("analysis_constraints", [])]
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("ranking_metric_id") in by_id:
            return by_id[rule["ranking_metric_id"]]
    for analysis in (rewrite.get("query_contract") or {}).get("analysis", []):
        for ranking in analysis.get("rankings", []):
            if ranking.get("metric_id") in by_id:
                return by_id[ranking["metric_id"]]
    matches = []
    for metric in rewrite.get("metrics", []):
        profile = by_id.get(metric.get("id"))
        positions = [question.find(term) for term in metric.get("matched_terms", []) if term and term in question]
        if profile and positions:
            matches.append((min(positions), profile))
    if matches:
        return min(matches, key=lambda item: item[0])[1]
    for field in info["order_fields"]:
        matched = next((item for item in numeric if item["name"] == field), None)
        if matched:
            return matched
    return next((by_id[item["id"]] for item in rewrite.get("metrics", []) if item.get("id") in by_id), numeric[0])


def _categories(profiles: list[dict], columns: list[str], rows: list[list[Any]]) -> list[str]:
    candidates = [item["name"] for item in profiles if item["kind"] in {"category", "date", "boolean"} and item["role"] not in {"id", "rank", "metric"}]
    selected = []
    for field in candidates[:3]:
        selected.append(field)
        keys = [tuple(str(row[columns.index(name)]) for name in selected) for row in rows]
        if len(set(keys)) == len(keys):
            return selected
    return []


def _relationship_metrics(profiles: list[dict], question: str) -> list[dict]:
    numeric = [item for item in profiles if item["kind"] == "numeric" and item["role"] == "metric" and item["unique_count"] > 1]
    clauses = [part for part in re.split(r"[。！？;；]", question) if part.strip()]
    text = clauses[-1] if clauses and re.search(r"关系|相关|散点|correlation|relationship", clauses[-1], re.I) else question
    terms = {
        "rating": ["评分", "rating", "score"], "amount": ["收入", "金额", "销售额", "revenue", "amount"],
        "people": ["人数", "people", "customers"], "tickets": ["票数", "tickets"],
        "orders": ["订单量", "订单数", "orders"], "reviews": ["评价数", "评价数量", "reviews"],
        "percent": ["比例", "占比", "percent", "ratio"],
    }
    matches = []
    for item in numeric:
        aliases = [item["name"], item["display_name"], *terms.get(item["unit"], [])]
        positions = [text.lower().find(term.lower()) for term in aliases if term and term.lower() in text.lower()]
        if positions:
            matches.append((min(positions), item))
    return [item for _, item in sorted(matches, key=lambda pair: pair[0])] if matches else numeric


def _complete_partition(info: dict, category: str, length: int) -> bool:
    """Prove only a direct single-category grouping, not arbitrary CTE semantics."""
    query = info["query"]
    if (not isinstance(query, exp.Select) or query.args.get("with_") or query.find(exp.Subquery) or query.find(exp.Window)
            or query.find(exp.Join) or len(list(query.find_all(exp.Table))) != 1):
        return False
    if query.args.get("where") or query.args.get("having") or query.args.get("distinct"):
        return False
    offset = query.args.get("offset")
    if offset and (not isinstance(offset.expression, exp.Literal) or not offset.expression.is_int or int(offset.expression.this) != 0):
        return False
    limit = query.args.get("limit")
    if limit and (not isinstance(limit.expression, exp.Literal) or not limit.expression.is_int or
                  int(limit.expression.this) <= length or limit.args.get("limit_options")):
        return False
    group = query.args.get("group")
    actual = info["expressions"].get(category)
    if group is None or len(group.expressions) != 1 or actual is None or any(value for key, value in group.args.items() if key != "expressions"):
        return False
    grouped = group.expressions[0]
    if isinstance(grouped, exp.Literal) and grouped.is_int:
        position = int(grouped.this) - 1
        grouped = query.selects[position] if 0 <= position < len(query.selects) else grouped
    elif isinstance(grouped, exp.Column) and not grouped.table and grouped.name == category:
        grouped = next((node for node in query.selects if node.alias_or_name == category), grouped)
    if isinstance(grouped, exp.Alias):
        grouped = grouped.this
    # Unqualified grouping vs a qualified projection are equivalent only when
    # their column names coincide and the direct query has one physical source.
    if isinstance(actual, exp.Column) and isinstance(grouped, exp.Column):
        sources = list(query.find_all(exp.Table))
        return actual.name == grouped.name and (actual.table == grouped.table or len(sources) == 1)
    return actual.sql(dialect="postgres") == grouped.sql(dialect="postgres")


def _same_observation(info: dict, axes: list[dict], rewrite: dict) -> bool:
    """Use single-source rows or an already validated shared-entity contract."""
    query = info["query"]
    joins = list(query.find_all(exp.Join))
    for join in joins:
        on = join.args.get("on")
        linked = join.args.get("using") or on is not None and on.find(exp.Or) is None and any(
            isinstance(pair.this, exp.Column) and isinstance(pair.expression, exp.Column) and
            pair.this.table and pair.expression.table and pair.this.table != pair.expression.table
            for pair in on.find_all(exp.EQ))
        if str(join.args.get("kind") or "").upper() == "CROSS" or not linked:
            return False
    scope = build_scope(query)
    tables = []
    seen = set()

    def visit(current: Scope) -> None:
        if id(current) in seen:
            return
        seen.add(id(current))
        for _, source in current.selected_sources.values():
            if isinstance(source, Scope):
                visit(source)
            elif isinstance(source, exp.Table):
                tables.append(source.name)

    if scope is not None:
        visit(scope)
    if len(tables) == 1 and not joins and query.find(exp.Subquery) is None:
        return True
    contract = rewrite.get("query_contract") or {}
    metric_ids = {axis.get("metric_id") for axis in axes}
    if None in metric_ids:
        return False
    for analysis in contract.get("analysis", []):
        if (analysis.get("shared_population") and analysis.get("population_entity_keys") and
                metric_ids.issubset({rank.get("metric_id") for rank in analysis.get("rankings", [])})):
            return True
    # Do not infer a grain for unknown joins or unrelated aggregate branches.
    return False


def plan_chart(
    question: str, sql: str, columns: list[str], rows: list[list[Any]], business_rewrite: dict | None = None,
    *, row_count: int | None = None, rows_truncated: bool = False,
) -> dict[str, Any]:
    """Return a validated v1 config or None plus local evidence and a concise reason.

    This planner never changes, sorts or aggregates result rows. Unknown and
    unsuitable shapes remain tables; a chart recommendation is not a SQL proof.
    """
    rewrite = business_rewrite or {}
    actual_count = len(rows) if isinstance(rows, (list, tuple)) else 0
    total = actual_count if row_count is None else row_count
    truncated = rows_truncated or isinstance(total, int) and total > actual_count
    intent = "relationship" if re.search(r"关系|相关|关联性|相关性|散点|correlation|relationship", question, re.I) else (
        "composition" if re.search(r"饼图|构成|组成|份额", question) or
        re.search(r"整体|总体|全部|总量|总数|总收入|总销售额", question) and re.search(r"占比|比例", question) else
        "trend" if re.search(r"趋势|走势|随时间|按月|逐月|每月|按日|逐日|按年|逐年|trend", question, re.I) else "comparison")
    result = {"config": None, "reason": {}, "intent": intent, "profile": {"columns": [], "row_count": total, "truncated": truncated}}

    def finish(code: str, message: str, config: dict | None = None) -> dict:
        result["reason"] = {"code": code, "message": message}
        if config is not None:
            try:
                result["config"] = validate_chart_config(config, columns)
            except AppError:
                result["reason"] = {"code": "invalid_config", "message": "图表字段无法安全绑定，请查看结果表。"}
        return result

    if (not isinstance(columns, (list, tuple)) or not columns or any(not isinstance(field, str) for field in columns)
            or len(set(columns)) != len(columns) or not isinstance(rows, (list, tuple))
            or any(not isinstance(row, (list, tuple)) or len(row) != len(columns) for row in rows)):
        return finish("ambiguous_columns", "结果字段重复或行结构不一致，请查看结果表。")
    if len(rows) < 2:
        return finish("insufficient_rows", "结果不足两行，直接查看表格更清楚。")
    try:
        info = _sql_info(sql, columns)
    except (ParseError, TokenError, OptimizeError, ValueError):
        return finish("unknown_sql_shape", "无法确认查询的字段结构，请查看结果表。")
    profiles = _profiles(columns, rows, info, rewrite)
    result["profile"]["columns"] = profiles
    primary = _primary(profiles, question, info, rewrite)
    if primary is None:
        return finish("no_numeric_metric", "没有可确认的数值指标，请查看结果表。")
    config = {"version": 1, "type": "bar", "title": primary["display_name"], "category": [], "series": [{"field": primary["name"], "name": primary["display_name"], "axis": "primary"}]}

    if intent == "relationship":
        numeric = _relationship_metrics(profiles, question)
        if len(numeric) != 2:
            return finish("ambiguous_relationship", "需要恰好两个非编号、非排名的连续数值指标才能画关系图，请查看结果表。")
        x, y = numeric
        if not _same_observation(info, numeric, rewrite):
            return finish("unconfirmed_observation_grain", "无法确认两项数值来自同一观察实体；笛卡尔组合或未知多来源粒度不能画关系图，请查看结果表。")
        pairs = [(_number(row[columns.index(x["name"])]), _number(row[columns.index(y["name"])])) for row in rows]
        if sum(a is not None and b is not None for a, b in pairs) < 2:
            return finish("insufficient_pairs", "可用数值配对不足，无法绘制关系图。")
        config.update(type="scatter", title=(x["display_name"] + "与" + y["display_name"] + "的关系")[:200], category=[x["name"]], series=[{"field": y["name"], "name": y["display_name"], "axis": "primary"}])
        return finish("numeric_relationship", "用同一结果行的两项数值展示关系；该图不证明相关性或因果关系。", config)

    if intent == "trend":
        times = [item for item in profiles if item["role"] == "time" and item["time_order"]]
        if len(times) != 1:
            return finish("unordered_or_grouped_time", "时间字段缺失、顺序不连续或同一时间有多组结果，当前单图无法准确展示趋势。")
        config.update(type="line", title=(primary["display_name"] + "趋势")[:200], category=[times[0]["name"]])
        return finish("ordered_time_trend", "时间值唯一且按时间顺序排列，使用折线图展示指标趋势。", config)

    categories = _categories(profiles, columns, rows)
    if not categories:
        return finish("no_unique_labels", "没有可区分每一行的分类标签，请查看结果表。")
    config["category"] = categories
    if intent == "composition":
        query = info["query"]
        top_n = bool(re.search(r"前\s*\d+|top\s*\d+|最高|最低|排名", question, re.I)) or query.find(exp.Window) is not None
        inner_limits = any(node.args.get("limit") or node.args.get("offset") for node in query.find_all(exp.Select) if node is not query)
        if truncated or top_n or inner_limits or len(categories) != 1 or not _complete_partition(info, categories[0], len(rows)):
            return finish("incomplete_composition", "饼图仅用于单事实表直接分类分组；关联、CTE、聚合后筛选、排名或截断结果不能确认完整互斥总体，请查看表格。")
        if len(categories) != 1 or not 2 <= len(rows) <= 6 or not primary["additive"]:
            return finish("non_additive_composition", "饼图需要少量互斥类别和可相加的指标；去重人数、均值或比例不能直接作为整体构成。")
        values = [_number(row[columns.index(primary["name"])]) for row in rows]
        labels = [row[columns.index(categories[0])] for row in rows]
        if any(value is None or value < 0 for value in values) or any(label is None for label in labels) or sum(values) <= 0:
            return finish("invalid_composition_values", "整体构成需要非空、非负且总量大于零的类别数据，请查看结果表。")
        config.update(type="pie", title=(primary["display_name"] + "构成")[:200])
        return finish("complete_additive_composition", "单事实表直接分组返回少量互斥类别，指标非负且可相加，使用饼图展示本查询结果范围的构成。", config)

    config["orientation"] = "horizontal" if len(rows) > 8 or any(len(str(row[columns.index(categories[-1])])) > 8 for row in rows) else "vertical"
    return finish("categorical_comparison", "分类或排名比较使用条形图；仅绘制主要指标，避免不同单位混在同一轴。", config)
