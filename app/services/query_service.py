import json
import re
import time

from app.core.errors import AppError
from app.core.config import settings
from app.core.logging import audit_log, get_logger
from app.models.contracts import DataSourceConfig, QueryExplainRequest, QueryRequest
from app.services.database import execute_select_sql
from app.services.business_rewrite_service import rewrite_business_question
from app.services.business_sql_service import validate_business_sql
from app.services.chart_planning_service import plan_chart
from app.services.chart_service import validate_chart_config
from app.services.schema_sql_validation import validate_schema_sql
from app.services.llm_service import (
    generate_explanation,
    generate_response,
    generate_sql,
)
from app.services.schema_cache_service import fetch_schema_with_cache, refresh_schema_cache
from app.services.schema_service import apply_semantic_config, build_schema_context, fetch_schema
from app.services.semantic_config_service import load_semantic_config_maps
from app.services.semantic_retrieval_service import retrieve_relevant_tables
from app.services.sql_security import (
    extract_used_columns,
    extract_used_tables,
    can_repair_readonly_output,
    reject_dangerous_intent,
    validate_and_normalize_sql,
)

logger = get_logger("app.audit.query")


def _can_retry_sql(exc: AppError) -> bool:
    if exc.code == 1003:
        if getattr(exc, "sqlstate", None) == "42501":
            return False
        return getattr(exc, "retryable_sql", True)
    if exc.code in {1027, 1028}:
        return True
    return can_repair_readonly_output(getattr(exc, "sql_candidate", ""), exc.code)


def _error_fields(exc: AppError, phase: str) -> dict:
    causes = [exc]
    while len(causes) < 4 and causes[-1].__cause__ is not None:
        causes.append(causes[-1].__cause__)
    return {
        "sql": next((getattr(item, "sql_candidate", None) for item in causes if getattr(item, "sql_candidate", None)), None),
        "error_message": exc.message,
        "error_code": exc.code, "error_type": exc.error_type,
        "phase": getattr(exc, "phase", phase),
        "sqlstate": next((getattr(item, "sqlstate", None) for item in causes if getattr(item, "sqlstate", None)), None),
        "business_failure_reason": getattr(exc, "business_failure_reason", None),
        "business_failure_details": getattr(exc, "business_failure_details", None),
    }


def _preserve_business_meaning(previous: dict, refreshed: dict) -> None:
    """A stale cache cannot turn a configured business query into an unguarded one."""
    lost_profile = not set(previous.get("profile_ids", [])).issubset(refreshed.get("profile_ids", []))
    changed = False
    for category, fields in (("metrics", ("table", "expression", "output_alias")),
                             ("dimensions", ("table", "column"))):
        current = {item.get("id"): item for item in refreshed.get(category, [])}
        for item in previous.get(category, []):
            replacement = current.get(item.get("id"))
            if replacement is None or any(item.get(field) != replacement.get(field) for field in fields):
                changed = True
    if lost_profile or changed:
        raise AppError(1026, "数据库结构已变化，原业务口径无法继续核对；请更新业务配置后重试。", "business_semantic_error", 422)


_BUSINESS_REPAIR_HINTS = {
    "join_fanout": "按每个指标的事实来源分别聚合，沿真实关联归属完整实体键后，每个实体键仅保留一行再关联。不要直接联结多方原始明细，也不要用SUM(DISTINCT 金额)掩盖重复。",
    "metric_formula": "定位指定output_alias的最终投影并逐层核对其来源；使用该指标配置的expression或alternatives，不要只改别名或加入未声明的NULL处理。",
    "count_source_ambiguity": "COUNT(*)需要证明关联后每条事实记录仍只有一行且事实来源不处于外连接可空侧；使用限定事实表的非空主键COUNT，遇一对多先聚合，不能只改别名或用票数代替订单量。",
    "nullable_aggregate_zero_fill": "AVG的空值与0不同。没有明确业务语义时保留原始平均值，移除擅自添加的零补齐；不要为了消除NULL将LEFT JOIN改成INNER JOIN而漏掉候选实体。",
    "rank_metric_mismatch": "保留未舍入指标作为排名层输入，窗口ORDER BY使用该原始指标及配置方向；同层展示别名不参与窗口排序，ROUND只放最外层展示。",
    "rank_population": "先按完整实体键形成候选集合并应用配置的数量门槛，再在同一层计算全部排名；不得提前LIMIT或先筛一种排名再计算另一种。",
    "rank_post_filter": "在窗口计算完成后的外层，对实际排名输出应用契约规定的比较方向和门槛；不能用OR绕过或先筛候选再排名。",
}


def _repair_feedback(exc: AppError) -> str:
    candidate = getattr(exc, "sql_candidate", "")
    parts = [exc.message]
    reason = getattr(exc, "business_failure_reason", None)
    if reason in _BUSINESS_REPAIR_HINTS:
        # Only validator-owned diagnostic fields enter the repair prompt. They
        # are evidence about a failed candidate, not a new user instruction.
        raw = getattr(exc, "business_failure_details", {})
        details = {key: raw[key] for key in (
            "metric_id", "output_alias", "source_table", "source_aliases", "tie_keys",
        ) if isinstance(raw, dict) and key in raw}
        parts.append("结构化失败原因（校验诊断）：" + json.dumps({"reason": reason, "details": details}, ensure_ascii=False))
        parts.append("针对该原因的修复步骤：" + _BUSINESS_REPAIR_HINTS[reason])
    if candidate:
        parts.append("上一条未通过的候选 SQL：\n" + candidate)
    return "\n".join(parts)


def _is_schema_question(question: str) -> bool:
    return bool(re.search(r"数据库.*(?:几|多少|哪些|什么).*表|有哪些表|表结构|字段结构|schema", question, re.IGNORECASE))


def _resolve_datasource(
    datasource: DataSourceConfig | None,
    data_source_id: str | None,
) -> tuple[DataSourceConfig, list[dict], str | None]:
    if data_source_id:
        resolved_datasource, schema = fetch_schema_with_cache(data_source_id)
        return resolved_datasource, schema, data_source_id
    if datasource is None:
        raise AppError(
            code=1012,
            message="缺少数据源配置",
            error_type="validation_error",
            status_code=422,
        )
    return datasource, fetch_schema(datasource), None


def _build_retrieved_schema_context(
    data_source_id: str | None,
    question: str,
    schema: list[dict],
    required_tables: list[str] | None = None,
) -> tuple[str, dict[str, object] | None]:
    if not data_source_id:
        return build_schema_context(schema), None
    retrieval = retrieve_relevant_tables(
        data_source_id=data_source_id,
        question=question,
        schema=schema,
    )
    selected_tables = retrieval.get("selected_tables") or []
    if not selected_tables:
        retrieval["fallback_mode"] = "full_schema_context"
        retrieval["selected_tables"] = [table["table_name"] for table in schema]
        return build_schema_context(schema), retrieval
    forced_tables = [name for name in required_tables or [] if name not in selected_tables]
    selected_tables = list(dict.fromkeys([*selected_tables, *forced_tables]))
    retrieval["selected_tables"] = selected_tables
    retrieval["business_required_tables"] = forced_tables
    table_set = set(selected_tables)
    filtered_schema = [table for table in schema if table["table_name"] in table_set]
    if not filtered_schema:
        retrieval["fallback_mode"] = "full_schema_context"
        retrieval["selected_tables"] = [table["table_name"] for table in schema]
        return build_schema_context(schema), retrieval
    return build_schema_context(filtered_schema), retrieval


def _prepare_business_query(
    question: str, datasource: DataSourceConfig, schema: list[dict], data_source_id: str | None,
) -> tuple[list[dict], dict]:
    if data_source_id:
        fields, tables = load_semantic_config_maps(data_source_id)
        schema = apply_semantic_config(schema, fields, tables)
    rewrite = rewrite_business_question(
        question=question, schema=schema, datasource=datasource, data_source_id=data_source_id,
    )
    return schema, rewrite


def _build_schema_meta_result(
    question: str,
    schema: list[dict],
    include_explanation: bool,
) -> dict:
    columns = ["table_name", "table_comment", "column_count", "columns"]
    rows = [
        [
            table["table_name"],
            table.get("table_comment") or "",
            len(table.get("columns") or []),
            ", ".join([col["name"] for col in table.get("columns") or []]),
        ]
        for table in schema
    ]
    table_names = [table["table_name"] for table in schema]
    data = {
        "sql": "-- 元数据问题: 安全规则禁止 SQL 查询 information_schema 等系统库, 本结果直接来自已缓存 Schema, 未执行 SQL",
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "used_tables": table_names,
        "safety_checks": None,
    }
    try:
        data["response"] = generate_response(
            question=question,
            sql=data["sql"],
            columns=columns,
            rows=rows,
            row_count=len(rows),
        )
    except AppError:
        data["response"] = "当前数据库共 {} 张表: ".format(len(rows)) + "; ".join(
            [f"{row[0]}({row[1]})" if row[1] else row[0] for row in rows]
        )
    if include_explanation:
        data["explanation"] = (
            f"问题“{question}”属于数据库结构元数据问题。"
            "安全规则禁止 SQL 访问 information_schema/pg_catalog 等系统库, "
            "因此本次未执行 SQL, 答复直接基于已注册数据源的缓存 Schema 生成。"
            f"库中共 {len(rows)} 张表: {'、'.join(table_names)}。"
        )
    else:
        data["explanation"] = None
    return data


def _run_once(
    question: str,
    datasource,
    schema_context: str,
    max_rows: int,
    include_explanation: bool,
    error_feedback: str | None = None,
    trace_id: str | None = None,
    business_rewrite: dict | None = None,
    schema: list[dict] | None = None,
) -> tuple[dict, dict[str, object]]:
    generated_sql, sql_prompt_meta = generate_sql(
        question=question,
        schema_context=schema_context,
        database_name=datasource.database,
        max_rows=max_rows,
        error_feedback=error_feedback,
        business_rewrite=business_rewrite,
    )
    try:
        safe_sql, checks = validate_and_normalize_sql(generated_sql, max_rows=max_rows)
    except AppError as exc:
        exc.sql_candidate = generated_sql
        exc.phase = "sql_validation"
        audit_log(
            logger,
            "sql_validation_failed",
            trace_id=trace_id,
            **_error_fields(exc, "sql_validation"),
        )
        raise
    if schema is not None:
        try:
            validate_schema_sql(safe_sql, schema, database_schema=settings.pg_schema)
        except AppError as exc:
            exc.sql_candidate = safe_sql
            exc.phase = "schema_validation"
            audit_log(logger, "schema_validation_failed", trace_id=trace_id, **_error_fields(exc, "schema_validation"))
            raise
    if business_rewrite:
        try:
            validate_business_sql(safe_sql, business_rewrite)
        except AppError as exc:
            exc.sql_candidate = safe_sql
            exc.phase = "business_validation"
            audit_log(
                logger, "business_validation_failed", trace_id=trace_id,
                **_error_fields(exc, "business_validation"),
            )
            raise
    try:
        columns, rows, row_count = execute_select_sql(datasource, safe_sql)
    except AppError as exc:
        exc.sql_candidate = safe_sql
        audit_log(
            logger, "database_query_failed", trace_id=trace_id, **_error_fields(exc, "database"),
        )
        raise
    used_tables = extract_used_tables(safe_sql)
    data = {
        "sql": safe_sql,
        "columns": columns,
        "rows": rows,
        "row_count": row_count,
        "used_tables": used_tables,
        "safety_checks": checks.to_dict(),
        "business_rewrite": business_rewrite,
    }
    data["response"] = generate_response(
        question=question,
        sql=safe_sql,
        columns=columns,
        rows=rows,
        row_count=row_count,
        business_rewrite=business_rewrite,
    )
    if include_explanation:
        data["explanation"] = generate_explanation(
            question=question,
            sql=safe_sql,
            used_tables=used_tables,
            safety_checks=checks.to_dict(),
            row_count=row_count,
            rewritten_question=(business_rewrite or {}).get("rewritten_question") or sql_prompt_meta.get("rewritten_question"),
            few_shot_categories=sql_prompt_meta.get("few_shot_categories"),
        )
    else:
        data["explanation"] = None
    return data, sql_prompt_meta


def execute_query(request: QueryRequest, trace_id: str) -> dict:
    started = time.perf_counter()
    retry_used = False
    try:
        reject_dangerous_intent(request.question)
    except AppError as exc:
        audit_log(
            logger,
            "query_rejected",
            trace_id=trace_id,
            question=request.question,
            error_type=exc.error_type,
            error_code=exc.code,
        )
        raise
    try:
        datasource, schema, resolved_data_source_id = _resolve_datasource(
            datasource=request.datasource, data_source_id=request.data_source_id,
        )
    except AppError as exc:
        audit_log(logger, "datasource_failed", trace_id=trace_id, **_error_fields(exc, "datasource"))
        raise
    if not schema:
        audit_log(
            logger,
            "query_failed",
            trace_id=trace_id,
            question=request.question,
            error_type="db_error",
            error_code=1009,
        )
        raise AppError(
            code=1009,
            message="目标数据库未读取到可用数据表",
            error_type="db_error",
            status_code=400,
        )
    try:
        schema, business_rewrite = _prepare_business_query(request.question, datasource, schema, resolved_data_source_id)
    except AppError as exc:
        audit_log(logger, "business_rewrite_failed", trace_id=trace_id, **_error_fields(exc, "business_rewrite"))
        raise
    schema_context, retrieval_meta = _build_retrieved_schema_context(
        data_source_id=resolved_data_source_id,
        question=business_rewrite["rewritten_question"],
        schema=schema,
        required_tables=business_rewrite["required_tables"],
    )
    meta_answered = False
    max_attempts = settings.sql_generation_max_attempts if request.options.retry_on_error else 1
    failures = []
    error_feedback = None
    schema_refresh_attempted = False
    schema_refreshed = False
    for attempt in range(max_attempts):
        try:
            result, sql_prompt_meta = _run_once(
                question=request.question,
                datasource=datasource,
                schema_context=schema_context,
                max_rows=request.options.max_rows,
                include_explanation=request.options.include_explanation,
                error_feedback=error_feedback,
                trace_id=trace_id,
                business_rewrite=business_rewrite,
                schema=schema,
            )
            break
        except AppError as exc:
            if exc.code == 2003 and _is_schema_question(request.question):
                meta_answered = True
                result = _build_schema_meta_result(request.question, schema, request.options.include_explanation)
                sql_prompt_meta = {}
                break
            failures.append({"error_code": exc.code, "error_type": exc.error_type})
            if attempt + 1 >= max_attempts or not _can_retry_sql(exc):
                audit_log(logger, "query_failed", trace_id=trace_id, question=request.question,
                          attempt_count=attempt + 1, **_error_fields(exc, "query"))
                raise
            if (resolved_data_source_id and not schema_refresh_attempted
                    and exc.code == 1003 and getattr(exc, "sqlstate", None) in {"42703", "42P01"}):
                schema_refresh_attempted = True
                try:
                    refresh_schema_cache(resolved_data_source_id)
                    fresh_datasource, fresh_schema = fetch_schema_with_cache(resolved_data_source_id)
                    if not fresh_schema:
                        raise AppError(1009, "刷新后未读取到可见业务表。", "db_error", 400)
                    fresh_schema, fresh_rewrite = _prepare_business_query(
                        request.question, fresh_datasource, fresh_schema, resolved_data_source_id,
                    )
                    _preserve_business_meaning(business_rewrite, fresh_rewrite)
                except Exception as refresh_problem:
                    refresh_error = refresh_problem if isinstance(refresh_problem, AppError) else AppError(
                        1003, f"Schema缓存刷新失败: {refresh_problem}", "schema_refresh_error", 503,
                    )
                    refresh_error.sql_candidate = getattr(exc, "sql_candidate", None)
                    refresh_error.phase = "schema_refresh"
                    if refresh_error.code in {1025, 1026}:
                        refresh_error.sqlstate = getattr(exc, "sqlstate", None)
                    audit_log(logger, "schema_refresh_failed", trace_id=trace_id,
                              data_source_id=resolved_data_source_id, **_error_fields(refresh_error, "schema_refresh"))
                    if refresh_error.code in {1025, 1026}:
                        raise refresh_error from exc
                    # A connection/cache refresh failure cannot repair the SQL;
                    # keep the original database error as the primary failure.
                    raise exc
                datasource, schema, business_rewrite = fresh_datasource, fresh_schema, fresh_rewrite
                schema_refreshed = True
                if retrieval_meta is not None:
                    retrieval_meta["fallback_mode"] = "refreshed_full_schema_context"
                    retrieval_meta["selected_tables"] = [table["table_name"] for table in schema]
                audit_log(logger, "schema_refreshed", trace_id=trace_id,
                          data_source_id=resolved_data_source_id, table_count=len(schema), phase="schema_refresh")
            retry_used = True
            error_feedback = _repair_feedback(exc)
            # Repair uses all visible tables, so retrieval cannot hide a missing join target.
            schema_context = build_schema_context(schema)
            audit_log(logger, "sql_repair_requested", trace_id=trace_id,
                      attempt=attempt + 1, **_error_fields(exc, "repair"))
    result["sql_generation"] = {"attempt_count": attempt + 1, "repaired": retry_used, "failures": failures,
                                "schema_refresh_attempted": schema_refresh_attempted, "schema_refreshed": schema_refreshed}
    result["business_rewrite"] = business_rewrite
    if retrieval_meta:
        result["retrieval"] = retrieval_meta
    result["chart_config"] = None
    result["chart_error"] = None
    result["chart_selection"] = None
    if request.options.include_chart and not meta_answered:
        try:
            selection = plan_chart(
                question=request.question,
                sql=result["sql"],
                columns=result["columns"],
                rows=result["rows"],
                row_count=result["row_count"],
                rows_truncated=result["row_count"] >= request.options.max_rows,
                business_rewrite=business_rewrite,
            )
            result["chart_selection"] = {key: selection[key] for key in ("intent", "reason", "profile")}
            if selection["config"] is not None:
                result["chart_config"] = validate_chart_config(selection["config"], result["columns"])
            audit_log(logger, "chart_selected", trace_id=trace_id,
                      chart_type=(result["chart_config"] or {}).get("type"),
                      chart_intent=selection["intent"], chart_reason=selection["reason"])
        except Exception as exc:
            result["chart_error"] = "图表暂时无法生成，查询结果仍可查看。"
            audit_log(
                logger,
                "chart_failed",
                trace_id=trace_id,
                error_type=exc.error_type if isinstance(exc, AppError) else "chart_planning_error",
                error_code=exc.code if isinstance(exc, AppError) else 1014,
            )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    result["elapsed_ms"] = elapsed_ms
    audit_log(
        logger,
        "query_succeeded",
        trace_id=trace_id,
        question=request.question,
        sql=result["sql"],
        rewritten_question=sql_prompt_meta.get("rewritten_question"),
        few_shot_categories=sql_prompt_meta.get("few_shot_categories"),
        mapping_count=len(sql_prompt_meta.get("mappings") or []),
        safety_checks=result.get("safety_checks"),
        elapsed_ms=elapsed_ms,
        retry_used=retry_used,
        meta_answered=meta_answered,
        row_count=result.get("row_count"),
        include_chart=request.options.include_chart,
        business_profile_ids=business_rewrite["profile_ids"],
        business_metric_ids=[metric["id"] for metric in business_rewrite["metrics"]],
        chart_type=(result["chart_config"] or {}).get("type"),
        chart_intent=(result["chart_selection"] or {}).get("intent"),
    )
    return result


def explain_query(request: QueryExplainRequest, trace_id: str) -> dict:
    try:
        reject_dangerous_intent(request.question)
    except AppError as exc:
        audit_log(
            logger,
            "query_explain_rejected",
            trace_id=trace_id,
            question=request.question,
            error_type=exc.error_type,
            error_code=exc.code,
        )
        raise
    try:
        datasource, schema, resolved_data_source_id = _resolve_datasource(
            datasource=request.datasource, data_source_id=request.data_source_id,
        )
    except AppError as exc:
        audit_log(logger, "datasource_failed", trace_id=trace_id, **_error_fields(exc, "datasource"))
        raise
    if not schema:
        audit_log(
            logger,
            "query_explain_failed",
            trace_id=trace_id,
            question=request.question,
            error_type="db_error",
            error_code=1010,
        )
        raise AppError(
            code=1010,
            message="目标数据库未读取到可用数据表",
            error_type="db_error",
            status_code=400,
        )
    try:
        schema, business_rewrite = _prepare_business_query(request.question, datasource, schema, resolved_data_source_id)
    except AppError as exc:
        audit_log(logger, "business_rewrite_failed", trace_id=trace_id, **_error_fields(exc, "business_rewrite"))
        raise
    schema_context, _ = _build_retrieved_schema_context(
        data_source_id=resolved_data_source_id,
        question=business_rewrite["rewritten_question"],
        schema=schema,
        required_tables=business_rewrite["required_tables"],
    )
    try:
        error_feedback = None
        for attempt in range(settings.sql_generation_max_attempts):
            candidate_sql = ""
            try:
                candidate_sql, sql_prompt_meta = generate_sql(
                    question=request.question,
                    schema_context=schema_context,
                    database_name=datasource.database,
                    max_rows=200,
                    business_rewrite=business_rewrite,
                    error_feedback=error_feedback,
                )
                normalized_sql, _ = validate_and_normalize_sql(candidate_sql, max_rows=200)
                validate_schema_sql(normalized_sql, schema, database_schema=settings.pg_schema)
                validate_business_sql(normalized_sql, business_rewrite)
                break
            except AppError as exc:
                exc.sql_candidate = candidate_sql
                audit_log(logger, "query_explain_validation_failed", trace_id=trace_id, **_error_fields(exc, "explain_validation"))
                if attempt + 1 < settings.sql_generation_max_attempts and _can_retry_sql(exc):
                    error_feedback = _repair_feedback(exc)
                    schema_context = build_schema_context(schema)
                    continue
                raise
    except AppError as exc:
        if exc.code != 2003 or not _is_schema_question(request.question):
            raise
        table_names = [table["table_name"] for table in schema]
        return {
            "candidate_sql": "",
            "reasoning": (
                f"问题“{request.question}”属于数据库结构元数据问题, "
                "安全规则禁止 SQL 访问 information_schema 等系统库, 故不生成候选 SQL。"
                f"库中共 {len(schema)} 张表: {'、'.join(table_names)}。"
            ),
            "used_tables": table_names,
            "used_columns": [],
            "business_rewrite": business_rewrite,
        }
    used_tables = extract_used_tables(normalized_sql)
    used_columns = extract_used_columns(normalized_sql)
    result = {
        "candidate_sql": normalized_sql,
        "reasoning": generate_explanation(
            question=request.question,
            sql=normalized_sql,
            used_tables=used_tables,
            safety_checks=None,
            row_count=None,
            rewritten_question=sql_prompt_meta.get("rewritten_question"),
            few_shot_categories=sql_prompt_meta.get("few_shot_categories"),
        ),
        "used_tables": used_tables,
        "used_columns": used_columns,
        "business_rewrite": business_rewrite,
        "sql_generation": {"attempt_count": attempt + 1, "repaired": attempt > 0},
    }
    audit_log(
        logger,
        "query_explain_succeeded",
        trace_id=trace_id,
        question=request.question,
        candidate_sql=normalized_sql,
        rewritten_question=sql_prompt_meta.get("rewritten_question"),
        few_shot_categories=sql_prompt_meta.get("few_shot_categories"),
        mapping_count=len(sql_prompt_meta.get("mappings") or []),
        used_tables=used_tables,
    )
    return result
