"""Resolve configured business terms against schema and observed database values.

The original question is retained. This stage adds deterministic, reviewable
constraints for SQL generation; it does not ask another model to invent meaning.
"""

import json
import re
from collections import deque
from pathlib import Path
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError, TokenError

from app.core.config import settings
from app.core.errors import AppError
from app.models.contracts import DataSourceConfig
from app.services.database import execute_select_sql
from app.services.query_contract_service import build_query_contract


BUSINESS_SEMANTICS_PATH = Path(__file__).resolve().parents[2] / "config" / "business_semantics.json"
ENUM_VALUE_LIMIT = 51


def _semantic_error(message: str) -> AppError:
    return AppError(1026, message, "business_semantic_error", 422)


def _load_profiles() -> list[dict[str, Any]]:
    try:
        payload = json.loads(BUSINESS_SEMANTICS_PATH.read_text(encoding="utf-8"))
        profiles = payload.get("profiles")
        if payload.get("version") != 1 or not isinstance(profiles, list):
            raise ValueError("version 必须为1，profiles 必须为数组")
        if any(not isinstance(profile, dict) for profile in profiles):
            raise ValueError("profile 必须为对象")
        return profiles
    except (OSError, ValueError, AttributeError) as exc:
        raise _semantic_error("业务语义配置不可用，请检查 config/business_semantics.json") from exc


def _schema_columns(schema: list[dict[str, Any]]) -> dict[str, set[str]]:
    return {
        str(table["table_name"]): {
            str(column["name"]) for column in table.get("columns", [])
        }
        for table in schema
        if table.get("table_name")
    }


def _profile_matches(
    profile: dict[str, Any],
    columns: dict[str, set[str]],
    data_source_id: str | None,
) -> bool:
    allowed_ids = profile.get("data_source_ids")
    if allowed_ids is not None and data_source_id not in allowed_ids:
        return False
    required = profile.get("required_columns")
    # An empty fingerprint must not activate a domain profile for every database.
    if not isinstance(required, dict) or not required:
        return False
    return all(
        table in columns and set(required_columns).issubset(columns[table])
        for table, required_columns in required.items()
    )


def _term_matches(question: str, term: str) -> list[tuple[int, int, str]]:
    if not term:
        return []
    # Latin aliases need boundaries: app must not match happy or application.
    prefix = r"(?<![A-Za-z0-9_])" if term[0].isascii() and term[0].isalnum() else ""
    suffix = r"(?![A-Za-z0-9_])" if term[-1].isascii() and term[-1].isalnum() else ""
    pattern = prefix + re.escape(term) + suffix
    return [(match.start(), match.end(), match.group()) for match in re.finditer(pattern, question, re.IGNORECASE)]


def _longest_matches(
    question: str, entries: list[tuple[str, dict[str, Any]]]
) -> list[tuple[str, dict[str, Any], str, int, int]]:
    candidates = []
    for order, (alias, entry) in enumerate(entries):
        for start, end, matched in _term_matches(question, alias):
            candidates.append((start, end, order, alias, entry, matched))
    candidates.sort(key=lambda candidate: (-(candidate[1] - candidate[0]), candidate[0], candidate[2]))
    meanings: dict[tuple[int, int], tuple[Any, ...]] = {}
    for start, end, _, _, entry, matched in candidates:
        meaning = (
            (entry.get("table"), entry.get("id"), entry.get("expression"), entry.get("output_alias"), tuple(entry.get("alternatives", [])))
            if "expression" in entry else (entry.get("table"), entry.get("column"), entry.get("value"))
        )
        if (start, end) in meanings and meanings[(start, end)] != meaning:
            raise _semantic_error(f"“{matched}”在匹配的业务配置中有不同定义，请明确所需字段或指标口径。")
        meanings[(start, end)] = meaning
    accepted = []
    spans: list[tuple[int, int]] = []
    for start, end, order, alias, entry, matched in candidates:
        if any(start < previous_end and end > previous_start for previous_start, previous_end in spans):
            continue
        spans.append((start, end))
        accepted.append((start, order, alias, entry, matched, end))
    accepted.sort(key=lambda candidate: (candidate[0], candidate[1]))
    return [(alias, entry, matched, start, end) for start, _, alias, entry, matched, end in accepted]


def _mapping_operator(question: str, start: int, end: int) -> str:
    """Preserve explicit exclusion in the clause containing this occurrence."""
    separators = r"[，,。.!！;；?？\n]"
    left = list(re.finditer(separators, question[:start]))
    clause_start = left[-1].end() if left else 0
    right = re.search(separators, question[end:])
    clause_end = end + right.start() if right else len(question)
    before, after = question[clause_start:start], question[end:clause_end]
    exclusions = list(re.finditer(r"排除|剔除|不包括|不包含|不含|除了", before))
    resets = list(re.finditer(r"之外|以外|统计|计算|比较|查看|分析|只看|仅看|保留|纳入|(?<!不)包括|(?<!不)包含", before))
    if exclusions and (not resets or exclusions[-1].start() > resets[-1].start()):
        return "exclude"
    suffix = re.search(r"不算|不计|除外|之外|以外", after)
    if suffix and not re.search(r"统计|计算|比较|查看|分析|只看|仅看|收入|金额|最高|最多|多少", after[:suffix.start()]):
        return "exclude"
    return "include"


def _require_columns(
    table: str, required: set[str], columns: dict[str, set[str]], label: str
) -> None:
    if table not in columns:
        raise _semantic_error(f"无法解析{label}：数据库缺少表 {table}。")
    missing = sorted(required - columns[table])
    if missing:
        raise _semantic_error(f"无法解析{label}：数据库缺少字段 {', '.join(f'{table}.{name}' for name in missing)}。")


def _metric_columns(metric: dict[str, Any]) -> set[str]:
    try:
        expressions = [metric["expression"], *metric.get("alternatives", [])]
        references: set[str] = set()
        for expression in expressions:
            parsed = parse_one(expression, read="postgres")
            references.update(column.name for column in parsed.find_all(exp.Column))
        return references
    except (KeyError, TypeError, ParseError, TokenError) as exc:
        raise _semantic_error(f"业务指标 {metric.get('id', '')} 的配置表达式无效。") from exc


def _quote_identifier(identifier: str) -> str:
    # Names are verified against fetched schema before use. Double quotes are
    # escaped even for unusual PostgreSQL identifiers; no user text enters SQL.
    return '"' + identifier.replace('"', '""') + '"'


def _observe_values(
    datasource: DataSourceConfig, table: str, column: str
) -> list[Any]:
    quoted_table = _quote_identifier(settings.pg_schema) + "." + _quote_identifier(table)
    quoted_column = _quote_identifier(column)
    sql = (
        f"SELECT DISTINCT {quoted_column} FROM {quoted_table} "
        f"WHERE {quoted_column} IS NOT NULL ORDER BY {quoted_column} LIMIT {ENUM_VALUE_LIMIT}"
    )
    try:
        _, rows, _ = execute_select_sql(datasource, sql)
    except AppError as exc:
        raise _semantic_error(f"无法确认 {table}.{column} 的业务名称映射，请检查数据源后重试。") from exc
    return [row[0] for row in rows[:ENUM_VALUE_LIMIT] if row]


def _add_join_paths(
    result: dict[str, Any], profiles: list[dict[str, Any]], columns: dict[str, set[str]]
) -> None:
    tables = result["required_tables"]
    if len(tables) < 2:
        return
    graph: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for profile in profiles:
        for relation in profile.get("joins", []):
            left, right = relation["left_table"], relation["right_table"]
            if left not in columns or right not in columns:
                continue
            graph.setdefault(left, []).append((right, relation))
            graph.setdefault(right, []).append((left, relation))
    anchor = result["metrics"][0]["table"] if result["metrics"] else tables[0]
    for target in list(tables):
        if target == anchor:
            continue
        queue = deque([(anchor, [anchor], [])])
        visited = {anchor}
        while queue:
            table, path_tables, joins = queue.popleft()
            if table == target:
                for relation in joins:
                    _require_columns(relation["left_table"], {relation["left_column"]}, columns, "已配置关联路径")
                    _require_columns(relation["right_table"], {relation["right_column"]}, columns, "已配置关联路径")
                result["join_paths"].append({"from_table": anchor, "to_table": target, "tables": path_tables, "joins": joins})
                tables.extend(table_name for table_name in path_tables if table_name not in tables)
                break
            for next_table, relation in graph.get(table, []):
                if next_table not in visited:
                    visited.add(next_table)
                    queue.append((next_table, [*path_tables, next_table], [*joins, relation]))


def _resolve_rank_filters(question: str, rule: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve only configured aliases, directions and numeric captures."""
    resolved: dict[str, dict[str, Any]] = {}
    try:
        for configured in rule.get("rank_filter_patterns", []):
            alias, operator = configured["output_alias"], configured["operator"]
            if operator not in {"lte", "gt"}:
                raise _semantic_error("业务配置的后置排名比较方向无效。")
            for pattern in configured.get("patterns", []):
                for match in re.finditer(pattern, question, re.IGNORECASE):
                    item = {"output_alias": alias, "operator": operator, "maximum": int(match.group(1))}
                    if alias in resolved and resolved[alias] != item:
                        raise _semantic_error(f"排名 {alias} 出现不同或冲突的后置比较门槛，请明确筛选条件。")
                    resolved[alias] = item
    except (KeyError, IndexError, ValueError, re.error) as exc:
        raise _semantic_error("业务配置的后置排名条件无法解析。") from exc
    return list(resolved.values())


def rewrite_business_question(
    question: str,
    schema: list[dict[str, Any]],
    datasource: DataSourceConfig,
    data_source_id: str | None = None,
) -> dict[str, Any]:
    """Return the preserved question and evidence-backed business constraints.

    Profile fingerprints/data-source IDs gate all domain rules. Enumerations are
    read only for aliases that occur in the question, once per matched field.
    Failures use 1026 so the caller can report a semantic issue before SQL runs.
    """
    result: dict[str, Any] = {
        "original_question": question,
        "rewritten_question": question,
        "value_mappings": [],
        "metrics": [],
        "assumptions": [],
        "required_tables": [],
        "profile_ids": [],
        "field_mappings": [],
        "source_evidence": [],
        "database_schema": settings.pg_schema,
        "dimensions": [],
        "join_paths": [],
        "interpretations": [],
        "analysis_constraints": [],
    }
    columns = _schema_columns(schema)
    profiles = [profile for profile in _load_profiles() if _profile_matches(profile, columns, data_source_id)]
    observed: dict[tuple[str, str], list[Any]] = {}
    metric_entries: list[tuple[str, dict[str, Any]]] = []
    value_entries: list[tuple[str, dict[str, Any]]] = []
    dimension_entries: list[tuple[str, dict[str, Any]]] = []
    for profile in profiles:
        profile_id = str(profile.get("id", ""))
        result["profile_ids"].append(profile_id)
        for term in profile.get("unsupported_terms", []):
            if _term_matches(question, term):
                raise _semantic_error(profile.get("unsupported_reason") or f"当前数据源未配置“{term}”的可靠计算口径。")
        for value_config in profile.get("values", []):
            for canonical, aliases in value_config.get("mappings", {}).items():
                entry = {"table": value_config["table"], "column": value_config["column"], "value": canonical, "profile_id": profile_id}
                if value_config.get("description"):
                    entry["description"] = value_config["description"]
                for alias in aliases:
                    context_terms = value_config.get("context_by_alias", {}).get(alias)
                    if context_terms and not any(_term_matches(question, term) for term in context_terms):
                        continue
                    value_entries.append((alias, entry))
        for metric in profile.get("metrics", []):
            context_terms = metric.get("context_any", [])
            if context_terms and not any(_term_matches(question, term) for term in context_terms):
                continue
            configured = {**metric, "profile_id": profile_id}
            metric_entries.extend((alias, configured) for alias in metric.get("aliases", []))
        for dimension in profile.get("dimensions", []):
            for alias in dimension.get("aliases", []):
                context_terms = dimension.get("context_by_alias", {}).get(alias, [])
                if context_terms and not any(_term_matches(question, term) for term in context_terms):
                    continue
                dimension_entries.append((alias, {**dimension, "profile_id": profile_id}))

    # Validate metric fields before enum reads, so a missing formula field does
    # not cause unnecessary database access.
    metrics_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for alias, metric, matched, _, _ in _longest_matches(question, metric_entries):
        if alias in metric.get("ambiguous_aliases", []) and any(
            _term_matches(question, context) for context in metric.get("ambiguous_context", [])
        ):
            raise _semantic_error(f"“{matched}”可能指订单票数或场次已售票数，请明确统计口径；两者来自不同字段，不能相互替代。")
        _require_columns(metric["table"], _metric_columns(metric), columns, f"指标“{matched}”")
        key = (metric["profile_id"], metric["id"])
        if key not in metrics_by_key:
            item = {**metric, "matched_terms": []}
            metrics_by_key[key] = item
            result["metrics"].append(item)
        if matched not in metrics_by_key[key]["matched_terms"]:
            metrics_by_key[key]["matched_terms"].append(matched)
        assumption = metric.get("assumptions_by_alias", {}).get(alias)
        if assumption and assumption not in result["assumptions"]:
            result["assumptions"].append(assumption)

    selected_values = _longest_matches(question, value_entries)
    directions: dict[tuple[str, str, str], str] = {}
    resolved_values = []
    for _, mapping, matched, start, end in selected_values:
        operator = _mapping_operator(question, start, end)
        key = (mapping["table"], mapping["column"], mapping["value"])
        if key in directions and directions[key] != operator:
            raise _semantic_error(f"“{matched}”同时出现包含与排除条件，请明确是否包含该业务值。")
        directions[key] = operator
        resolved_values.append(({**mapping, "operator": operator}, matched))

    for mapping, matched in resolved_values:
        table, column, canonical = mapping["table"], mapping["column"], mapping["value"]
        _require_columns(table, {column}, columns, f"业务名称“{matched}”")
        key = (table, column)
        if key not in observed:
            observed[key] = _observe_values(datasource, table, column)
            result["source_evidence"].append({"type": "observed_enum", "schema": settings.pg_schema, "table": table, "column": column, "limit": ENUM_VALUE_LIMIT, "observed_values": observed[key]})
        if canonical not in observed[key]:
            raise _semantic_error(f"无法确认“{matched}”对应 {table}.{column}={canonical!r}：实际数据中未观察到该值，已停止查询。")
        item = {**mapping, "term": matched, "observed_values": observed[key]}
        if not any(existing["table"] == table and existing["column"] == column and existing["term"] == matched for existing in result["value_mappings"]):
            result["value_mappings"].append(item)

    dimensions_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    dimension_matches = _longest_matches(question, dimension_entries)
    for _, dimension, matched, _, _ in dimension_matches:
        _require_columns(dimension["table"], {dimension["column"], *dimension.get("group_by", [])}, columns, f"业务维度“{matched}”")
        key = (dimension["table"], dimension["column"])
        if key not in dimensions_by_key:
            item = {**dimension, "matched_terms": [], "require_output": False}
            dimensions_by_key[key] = item
            result["dimensions"].append(item)
        if matched not in dimensions_by_key[key]["matched_terms"]:
            dimensions_by_key[key]["matched_terms"].append(matched)
    # Membership values also identify the dimension even when the user says
    # 普通、银卡、金卡和白金用户 without mentioning the field's formal name.
    for mapping in result["value_mappings"]:
        key = (mapping["table"], mapping["column"])
        for profile in profiles:
            for dimension in profile.get("dimensions", []):
                if key != (dimension["table"], dimension["column"]):
                    continue
                if key not in dimensions_by_key:
                    _require_columns(dimension["table"], {dimension["column"], *dimension.get("group_by", [])}, columns, "已配置业务维度")
                    item = {**dimension, "profile_id": profile["id"], "matched_terms": [], "require_output": False}
                    dimensions_by_key[key] = item
                    result["dimensions"].append(item)
                if mapping["term"] not in dimensions_by_key[key]["matched_terms"]:
                    dimensions_by_key[key]["matched_terms"].append(mapping["term"])

    metric_ids = {metric["id"] for metric in result["metrics"]}
    dimension_ids = {dimension["id"] for dimension in result["dimensions"]}
    for profile in profiles:
        for interpretation in profile.get("interpretations", []):
            if not set(interpretation.get("requires_dimension_ids", [])).issubset(dimension_ids):
                continue
            terms = [alias for alias in interpretation.get("aliases", []) if _term_matches(question, alias)]
            if not terms:
                continue
            missing = set(interpretation.get("requires_metric_ids", [])) - metric_ids
            if missing:
                raise _semantic_error("“喜欢”或“偏好”需要明确可查询的衡量口径；请说明是否按去重购票人数排名，不能推测用户心理。")
            result["interpretations"].append({**interpretation, "profile_id": profile["id"], "matched_terms": terms})
        for rule in profile.get("analysis_rules", []):
            if not set(rule.get("requires_dimension_ids", [])).issubset(dimension_ids):
                continue
            if not set(rule.get("requires_metric_ids", [])).issubset(metric_ids):
                continue
            terms = [alias for alias in rule.get("aliases", []) if _term_matches(question, alias)]
            if terms:
                item = {**rule, "profile_id": profile["id"], "matched_terms": terms}
                review_thresholds = {
                    int(match.group(1))
                    for pattern in rule.get("review_count_min_patterns", [])
                    for match in re.finditer(pattern, question)
                }
                if len(review_thresholds) > 1:
                    raise _semantic_error("观众评价数量出现不同的最低门槛，请明确候选电影的最低评价数量。")
                if review_thresholds:
                    item["review_count_min"] = next(iter(review_thresholds))
                if rule.get("rank_filter_patterns"):
                    item["resolved_rank_filters"] = _resolve_rank_filters(question, rule)
                result["analysis_constraints"].append(item)
                for assumption in rule.get("assumptions", []):
                    if assumption not in result["assumptions"]:
                        result["assumptions"].append(assumption)

    required_output_dimensions = {
        dimension_id
        for item in result["interpretations"] + result["analysis_constraints"]
        for dimension_id in item.get("requires_dimension_ids", [])
    }
    for dimension in result["dimensions"]:
        try:
            explicit_output_context = any(
                re.search(pattern, question, re.IGNORECASE)
                for pattern in dimension.get("output_context_patterns", [])
            )
        except re.error as exc:
            raise _semantic_error(f"业务维度 {dimension['id']} 的输出上下文配置无效。") from exc
        dimension["require_output"] = dimension["id"] in required_output_dimensions or explicit_output_context

    tables = [item["table"] for item in result["value_mappings"] + result["metrics"] + result["dimensions"]]
    result["required_tables"] = list(dict.fromkeys(tables))
    _add_join_paths(result, profiles, columns)
    if result["metrics"]:
        result["source_evidence"].append({"type": "configured_metrics", "configuration": "config/business_semantics.json", "metric_ids": [item["id"] for item in result["metrics"]]})
    annotations = []
    if result["value_mappings"]:
        annotations.append("业务名称映射（过滤条件使用数据库值，展示标签可使用业务名称）：")
        annotations.extend(
            f"- {item['term']} 对应 {item['table']}.{item['column']} {('<>' if item['operator'] == 'exclude' else '=')} {item['value']!r}；"
            f"方向为{'排除' if item['operator'] == 'exclude' else '包含'}，必须保留原问题的方向；已在实际数据中确认。"
            for item in result["value_mappings"]
        )
        annotations.extend(f"- {item['description']}" for item in result["value_mappings"] if item.get("description"))
    if result["metrics"]:
        annotations.append("已配置的指标口径（表达式中的字段来自对应表）：")
        annotations.extend(f"- {'、'.join(item['matched_terms'])}：{item['table']}，{item['expression']}，输出别名 {item['output_alias']}。{item['description']}" for item in result["metrics"])
        if any(item["table"] == "ticket_orders" for item in result["metrics"]):
            annotations.append("金额、订单数与平均每单实收必须保持一条订单一个统计单位；关联订单明细或评价等一对多表前先聚合，禁止重复累计订单金额，禁止用 SUM(DISTINCT 金额) 修补重复。购票人数不得使用票数代替。")
            annotations.append("除非原问题明确要求，不要新增排除退款订单等筛选条件；金额公式已扣除退款。")
    if result["dimensions"]:
        annotations.append("业务维度：")
        annotations.extend(
            f"- {'、'.join(item['matched_terms'])} 对应 {item['table']}.{item['column']}，"
            + (f"原问题要求按该维度展示，输出结果必须包含该维度；分组字段为 {', '.join(item.get('group_by') or [item['column']])}。"
               if item['require_output'] else "该维度用于理解业务对象或筛选；是否分组与输出依原问题，汇总问题无需输出该维度。")
            + item['description']
            for item in result["dimensions"]
        )
    if result["join_paths"]:
        annotations.append("经过字段核对的关联路径：")
        annotations.extend(
            "- " + " -> ".join(path["tables"]) + "：" + "；".join(
                f"{join['left_table']}.{join['left_column']} = {join['right_table']}.{join['right_column']} ({join['cardinality']})"
                for join in path["joins"]
            )
            for path in result["join_paths"]
        )
    if result["interpretations"] or result["analysis_constraints"]:
        annotations.append("分析与排名口径：")
        annotations.extend(f"- {item['description']}" for item in result["interpretations"] + result["analysis_constraints"])
        annotations.extend(
            f"- 候选电影必须先满足 review_count >= {item['review_count_min']}，再计算两项排名；该门槛使用聚合后的评价记录数。"
            for item in result["analysis_constraints"] if "review_count_min" in item
        )
        annotations.extend(
            f"- 排名计算完成后再应用条件：{condition['output_alias']} {'<=' if condition['operator'] == 'lte' else '>'} {condition['maximum']}，不能放宽阈值或颠倒比较方向。"
            for item in result["analysis_constraints"]
            for condition in item.get("resolved_rank_filters", [])
        )
    if result["assumptions"]:
        annotations.append("默认口径：" + "；".join(result["assumptions"]))
    if annotations:
        result["rewritten_question"] = question + "\n\n业务语义约束：\n" + "\n".join(annotations)
    result["query_contract"] = build_query_contract(result, schema)
    return result
