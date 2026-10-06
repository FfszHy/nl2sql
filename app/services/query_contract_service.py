"""Build a schema-scoped query contract from configured, resolved semantics.

This module does not interpret natural-language questions. Business terms and
analysis rules are supplied by the rewrite stage; PK/FK evidence is supplied by
database metadata. Declaring a contract does not verify a generated SQL query.
"""

from copy import deepcopy
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError, TokenError

from app.core.errors import AppError


def _reject(message: str) -> None:
    raise AppError(1026, message, "business_semantic_error", 422)


def _qualified_keys(values: list[Any], default_table: str) -> list[str]:
    keys = []
    for value in values:
        if isinstance(value, dict):
            table, column = value.get("table", default_table), value.get("column")
        else:
            parts = str(value).rsplit(".", 1)
            table, column = (parts[0], parts[1]) if len(parts) == 2 else (default_table, parts[0])
        if not table or not column:
            _reject("查询契约存在缺少表或字段的键配置。")
        key = f"{table}.{column}"
        if key not in keys:
            keys.append(key)
    return keys


def _validate_keys(keys: list[str], table_map: dict[str, dict[str, Any]]) -> None:
    for key in keys:
        table, column = key.rsplit(".", 1)
        columns = {item["name"] for item in table_map.get(table, {}).get("columns", [])}
        if table not in table_map or column not in columns:
            _reject(f"查询契约引用的字段不存在：{key}。")


def _primary_keys(table: dict[str, Any]) -> list[str]:
    return list(table.get("primary_key") or table.get("primary_keys") or [])


def _is_unique(table: dict[str, Any], columns: list[str]) -> bool:
    requested = set(columns)
    keys = [_primary_keys(table), *(table.get("unique_keys") or [])]
    return any(key and set(key) == requested for key in keys)


def _schema_key_evidence(table: dict[str, Any]) -> dict[str, Any]:
    visible = {column["name"] for column in table.get("columns", [])}
    primary_key = _primary_keys(table)
    if not primary_key or not set(primary_key).issubset(visible):
        primary_key = []
    unique_keys = []
    for key in table.get("unique_keys") or []:
        # Retain the complete composite key or no evidence at all. A visible
        # subset of a composite key does not establish column uniqueness.
        if key and set(key).issubset(visible) and list(key) not in unique_keys:
            unique_keys.append(list(key))
    version = table.get("schema_metadata_version")
    return {
        "primary_key": primary_key,
        "unique_keys": unique_keys,
        "metadata_verified": isinstance(version, int) and not isinstance(version, bool) and version >= 2,
    }


def _metric_contract(
    metric: dict[str, Any], database_schema: str, table_map: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    table_name = str(metric["table"])
    table = table_map.get(table_name)
    if table is None:
        _reject(f"查询契约的指标数据表不存在：{table_name}。")
    configured_grain = metric.get("grain_keys") or []
    grain_keys = _qualified_keys(configured_grain or _primary_keys(table), table_name)
    _validate_keys(grain_keys, table_map)
    source_columns = []
    expressions = [str(metric["expression"]), *map(str, metric.get("alternatives", []))]
    try:
        for expression in expressions:
            for column in parse_one(expression, read="postgres").find_all(exp.Column):
                key = f"{column.table or table_name}.{column.name}"
                if key not in source_columns:
                    source_columns.append(key)
    except (ParseError, TokenError) as exc:
        raise AppError(1026, "查询契约的指标表达式无法解析。", "business_semantic_error", 422) from exc
    _validate_keys(source_columns, table_map)
    nullability = {}
    for key in source_columns:
        source_table, source_column = key.rsplit(".", 1)
        column = next(item for item in table_map[source_table].get("columns", []) if item["name"] == source_column)
        recorded = column.get("nullable")
        nullability[key] = recorded if recorded in {"YES", "NO"} else "UNKNOWN"
    return {
        "id": metric["id"],
        "output_alias": metric["output_alias"],
        "expression": expressions[0],
        "alternatives": expressions[1:],
        "source": {"schema": database_schema, "table": table_name, "columns": source_columns, "nullability": nullability},
        "grain_keys": grain_keys,
        "grain_evidence": "configured" if configured_grain else "database_primary_key" if grain_keys else "unspecified",
        "database_primary_key": _qualified_keys(_primary_keys(table), table_name),
        "unsafe_join_tables": list(metric.get("unsafe_join_tables") or []),
        "display_name": metric.get("display_name") or metric["output_alias"],
        "unit": metric.get("unit"),
        "additive": metric.get("additive") if isinstance(metric.get("additive"), bool) else None,
    }


def _entity_contract(
    dimension: dict[str, Any], database_schema: str, table_map: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    table_name = str(dimension["table"])
    table = table_map.get(table_name)
    if table is None:
        _reject(f"查询契约的维度数据表不存在：{table_name}。")
    configured_keys = dimension.get("entity_keys") or []
    entity_keys = _qualified_keys(configured_keys or _primary_keys(table) or dimension.get("group_by") or [dimension["column"]], table_name)
    group_by = _qualified_keys(dimension.get("group_by") or [dimension["column"]], table_name)
    output_column = f"{table_name}.{dimension['column']}"
    _validate_keys([*entity_keys, *group_by, output_column], table_map)
    source_keys = [key.rsplit(".", 1)[1] for key in entity_keys if key.rsplit(".", 1)[0] == table_name]
    return {
        "id": dimension["id"],
        "source": {"schema": database_schema, "table": table_name},
        "output_column": output_column,
        "require_output": bool(dimension.get("require_output", False)),
        "entity_keys": entity_keys,
        "group_by": group_by,
        "key_evidence": "configured" if configured_keys else "database_primary_key" if _primary_keys(table) else "configured_grouping",
        "database_unique": len(source_keys) == len(entity_keys) and _is_unique(table, source_keys),
    }


def _join_contract(
    relation: dict[str, Any], database_schema: str, table_map: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    left_table, right_table = str(relation["left_table"]), str(relation["right_table"])
    left_columns = list(relation.get("left_columns") or [relation["left_column"]])
    right_columns = list(relation.get("right_columns") or [relation["right_column"]])
    left_keys = _qualified_keys(left_columns, left_table)
    right_keys = _qualified_keys(right_columns, right_table)
    _validate_keys([*left_keys, *right_keys], table_map)
    if len(left_keys) != len(right_keys):
        _reject("查询契约的关联两侧字段数量不一致。")
    observed_fk = next((
        fk for fk in table_map[left_table].get("foreign_keys", [])
        if fk.get("columns") == left_columns
        and fk.get("referenced_table") == right_table
        and fk.get("referenced_schema", database_schema) == database_schema
        and fk.get("referenced_columns") == right_columns
    ), None)
    right_unique = _is_unique(table_map[right_table], right_columns)
    configured_cardinality = relation.get("cardinality")
    observed_cardinality = (
        observed_fk.get("cardinality") or ("one_to_one" if _is_unique(table_map[left_table], left_columns) else "many_to_one")
        if observed_fk else None
    )
    return {
        "left": {"schema": database_schema, "table": left_table, "columns": left_columns},
        "right": {"schema": database_schema, "table": right_table, "columns": right_columns},
        "cardinality": observed_cardinality or configured_cardinality or "unknown",
        "configured_cardinality": configured_cardinality,
        "database_cardinality": observed_cardinality,
        "evidence": {
            "kind": "database_foreign_key" if observed_fk else "configured_relationship",
            "columns_verified": True,
            "foreign_key_verified": observed_fk is not None,
            "referenced_key_unique": right_unique,
            "constraint_name": observed_fk.get("constraint_name") if observed_fk else None,
        },
    }


def _analysis_contract(
    rule: dict[str, Any], metrics: dict[str, dict[str, Any]], entities: dict[str, dict[str, Any]], table_map: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    configured = rule.get("rank_contract")
    if not configured:
        return {
            "id": rule["id"], "kind": "described_analysis",
            "description": rule.get("description", ""),
            "validation_scope": "description_only",
        }
    contract = {**deepcopy(configured), "id": rule["id"], "validation_scope": "declared_contract"}
    if contract.get("kind") != "parallel_rankings":
        _reject("查询契约包含暂不支持的结构化排名类型。")
    ranking_aliases = set()
    for ranking in contract.get("rankings", []):
        metric = metrics.get(ranking.get("metric_id"))
        if metric is None:
            _reject("结构化排名引用了未匹配的业务指标。")
        alias = ranking.get("output_alias")
        if not alias or alias in ranking_aliases:
            _reject("结构化排名缺少或重复输出别名。")
        ranking_aliases.add(alias)
        if ranking.get("direction", "desc") not in {"asc", "desc"}:
            _reject("结构化排名的排序方向配置无效。")
        ranking["direction"] = ranking.get("direction", "desc")
        ranking["metric_output_alias"] = metric["output_alias"]
        ranking["partition_by"] = list(ranking.get("partition_by") or [])
        _validate_keys(ranking["partition_by"], table_map)
    if not ranking_aliases or contract.get("policy") not in {"ROW_NUMBER", "RANK", "DENSE_RANK"}:
        _reject("结构化排名必须包含有效排名及排名策略。")
    resolved_filters: dict[str, dict[str, Any]] = {}
    for condition in rule.get("resolved_rank_filters", []):
        alias, operator, maximum = condition.get("output_alias"), condition.get("operator"), condition.get("maximum")
        if alias not in ranking_aliases or operator not in {"lte", "gt"}:
            _reject("后置排名比较引用了未知排名或无效比较方向。")
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
            _reject("后置排名比较必须使用明确的非负整数门槛。")
        normalized = {"output_alias": alias, "operator": operator, "maximum": maximum}
        if alias in resolved_filters and resolved_filters[alias] != normalized:
            _reject("同一排名存在冲突的后置比较条件。")
        resolved_filters[alias] = normalized
    contract["post_rank_filters"] = list(resolved_filters.values())
    contract["tie_keys"] = list(contract.get("tie_keys") or [])
    _validate_keys(contract["tie_keys"], table_map)
    entity_id = contract.get("population_entity_id")
    if entity_id:
        if entity_id not in entities:
            _reject("结构化排名引用了未匹配的候选实体。")
        contract["population_entity_keys"] = entities[entity_id]["entity_keys"]
    minimum = contract.get("population_min_count")
    if minimum:
        metric = metrics.get(minimum.get("metric_id"))
        if metric is None:
            _reject("结构化排名的候选门槛引用了未匹配的计数指标。")
        threshold_key = minimum.get("threshold_key")
        threshold = rule.get(threshold_key) if threshold_key else minimum.get("minimum")
        if threshold is None:
            # A rule without an explicit threshold still declares the ranking
            # population, but must not invent a numeric qualification.
            contract.pop("population_min_count")
        else:
            if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
                _reject("结构化排名的候选数量门槛配置无效。")
            contract["population_min_count"] = {
                "metric_id": metric["id"], "output_alias": metric["output_alias"],
                "operator": "gte", "minimum": threshold,
            }
    return contract


def build_query_contract(rewrite: dict[str, Any], schema: list[dict[str, Any]]) -> dict[str, Any]:
    """Build version 1 without embedding domain names or full user questions."""
    table_map = {str(table["table_name"]): table for table in schema if table.get("table_name")}
    database_schema = str(rewrite.get("database_schema") or "public")
    metrics = [_metric_contract(metric, database_schema, table_map) for metric in rewrite.get("metrics", [])]
    entities = [_entity_contract(dimension, database_schema, table_map) for dimension in rewrite.get("dimensions", [])]
    metric_map, entity_map = {metric["id"]: metric for metric in metrics}, {entity["id"]: entity for entity in entities}
    joins = []
    seen_joins = set()
    for path in rewrite.get("join_paths", []):
        for relation in path.get("joins", []):
            join = _join_contract(relation, database_schema, table_map)
            signature = (join["left"]["table"], tuple(join["left"]["columns"]), join["right"]["table"], tuple(join["right"]["columns"]))
            if signature not in seen_joins:
                seen_joins.add(signature)
                joins.append(join)
    filters = []
    for mapping in rewrite.get("value_mappings", []):
        _validate_keys([f"{mapping['table']}.{mapping['column']}"], table_map)
        filters.append({
            "source": {"schema": database_schema, "table": mapping["table"], "column": mapping["column"]},
            "operator": mapping.get("operator", "include"), "value": mapping["value"],
            "evidence": "observed_enum" if mapping["value"] in mapping.get("observed_values", []) else "configured",
        })
    return {
        "version": 1, "database_schema": database_schema,
        "metrics": metrics, "entities": entities, "joins": joins, "filters": filters,
        "analysis": [_analysis_contract(rule, metric_map, entity_map, table_map) for rule in rewrite.get("analysis_constraints", [])],
        "schema_metadata_versions": {name: table.get("schema_metadata_version") for name, table in table_map.items() if table.get("schema_metadata_version") is not None},
        "schema_keys": {name: _schema_key_evidence(table) for name, table in table_map.items()},
    }
