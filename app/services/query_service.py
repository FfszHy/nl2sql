import time

from app.core.errors import AppError
from app.core.logging import audit_log, get_logger
from app.models.contracts import DataSourceConfig, QueryExplainRequest, QueryRequest
from app.services.database import execute_select_sql
from app.services.llm_service import (
    generate_echarts_code,
    generate_explanation,
    generate_response,
    generate_sql,
)
from app.services.schema_cache_service import fetch_schema_with_cache
from app.services.schema_service import build_schema_context, fetch_schema
from app.services.semantic_retrieval_service import retrieve_relevant_tables
from app.services.sql_security import (
    extract_used_columns,
    extract_used_tables,
    reject_dangerous_intent,
    validate_and_normalize_sql,
)

logger = get_logger("app.audit.query")


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
    table_set = set(selected_tables)
    filtered_schema = [table for table in schema if table["table_name"] in table_set]
    if not filtered_schema:
        retrieval["fallback_mode"] = "full_schema_context"
        retrieval["selected_tables"] = [table["table_name"] for table in schema]
        return build_schema_context(schema), retrieval
    return build_schema_context(filtered_schema), retrieval


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
) -> tuple[dict, dict[str, object]]:
    generated_sql, sql_prompt_meta = generate_sql(
        question=question,
        schema_context=schema_context,
        database_name=datasource.database,
        max_rows=max_rows,
        error_feedback=error_feedback,
    )
    try:
        safe_sql, checks = validate_and_normalize_sql(generated_sql, max_rows=max_rows)
    except AppError as exc:
        audit_log(
            logger,
            "sql_validation_failed",
            trace_id=trace_id,
            sql=generated_sql,
            error_type=exc.error_type,
            error_code=exc.code,
        )
        raise
    columns, rows, row_count = execute_select_sql(datasource, safe_sql)
    used_tables = extract_used_tables(safe_sql)
    data = {
        "sql": safe_sql,
        "columns": columns,
        "rows": rows,
        "row_count": row_count,
        "used_tables": used_tables,
        "safety_checks": checks.to_dict(),
    }
    data["response"] = generate_response(
        question=question,
        sql=safe_sql,
        columns=columns,
        rows=rows,
        row_count=row_count,
    )
    if include_explanation:
        data["explanation"] = generate_explanation(
            question=question,
            sql=safe_sql,
            used_tables=used_tables,
            safety_checks=checks.to_dict(),
            row_count=row_count,
            rewritten_question=sql_prompt_meta.get("rewritten_question"),
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
    datasource, schema, resolved_data_source_id = _resolve_datasource(
        datasource=request.datasource,
        data_source_id=request.data_source_id,
    )
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
    schema_context, retrieval_meta = _build_retrieved_schema_context(
        data_source_id=resolved_data_source_id,
        question=request.question,
        schema=schema,
    )
    meta_answered = False
    try:
        result, sql_prompt_meta = _run_once(
            question=request.question,
            datasource=datasource,
            schema_context=schema_context,
            max_rows=request.options.max_rows,
            include_explanation=request.options.include_explanation,
            trace_id=trace_id,
        )
    except AppError as exc:
        if exc.code == 2003:
            meta_answered = True
            result = _build_schema_meta_result(
                question=request.question,
                schema=schema,
                include_explanation=request.options.include_explanation,
            )
            sql_prompt_meta = {}
        elif not request.options.retry_on_error or exc.error_type == "sql_security_error":
            audit_log(
                logger,
                "query_failed",
                trace_id=trace_id,
                question=request.question,
                error_type=exc.error_type,
                error_code=exc.code,
            )
            raise
        else:
            retry_used = True
            result, sql_prompt_meta = _run_once(
                question=request.question,
                datasource=datasource,
                schema_context=schema_context,
                max_rows=request.options.max_rows,
                include_explanation=request.options.include_explanation,
                error_feedback=exc.message,
                trace_id=trace_id,
            )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    result["elapsed_ms"] = elapsed_ms
    if retrieval_meta:
        result["retrieval"] = retrieval_meta
    if request.options.include_chart and not meta_answered:
        result["echarts_code"] = generate_echarts_code(
            question=request.question,
            sql=result["sql"],
            columns=result["columns"],
            row_count=result["row_count"],
            explanation=result.get("explanation"),
        )
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
    datasource, schema, resolved_data_source_id = _resolve_datasource(
        datasource=request.datasource,
        data_source_id=request.data_source_id,
    )
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
    schema_context, _ = _build_retrieved_schema_context(
        data_source_id=resolved_data_source_id,
        question=request.question,
        schema=schema,
    )
    try:
        candidate_sql, sql_prompt_meta = generate_sql(
            question=request.question,
            schema_context=schema_context,
            database_name=datasource.database,
            max_rows=200,
        )
        normalized_sql, _ = validate_and_normalize_sql(candidate_sql, max_rows=200)
    except AppError as exc:
        if exc.code != 2003:
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
