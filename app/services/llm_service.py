import json
import re
import urllib.error
import urllib.request
from typing import Any

from app.core.config import settings
from app.core.errors import AppError
from app.services.business_sql_service import business_result_dimensions

COLLOQUIAL_MAPPINGS = [
    ("重载", "heavy_load_count"),
    ("过载", "overload_count"),
    ("三相不平衡", "three_phase_imbalance_count"),
]

FEW_SHOT_EXAMPLES = [
    {
        "category": "top_n",
        "triggers": ["近", "最高", "top", "排名"],
        "question": "近一年票房最高的电影有哪些",
        "sql": "SELECT title, box_office_million FROM movies WHERE release_date >= CURRENT_DATE - INTERVAL '365 days' ORDER BY box_office_million DESC LIMIT 20",
    },
    {
        "category": "group_by",
        "triggers": ["按", "统计", "分组"],
        "question": "按类型统计电影数量",
        "sql": "SELECT genre, COUNT(*) AS movie_count FROM movies GROUP BY genre ORDER BY movie_count DESC LIMIT 200",
    },
    {
        "category": "filter",
        "triggers": ["大于", "有哪些", "筛选"],
        "question": "评分大于8的电影有哪些",
        "sql": "SELECT title, imdb_score FROM movies WHERE imdb_score > 8 ORDER BY imdb_score DESC LIMIT 200",
    },
]


def _strip_wrappers(text: str, fence_language: str | None = None) -> str:
    content = text.strip()
    if fence_language:
        content = re.sub(rf"^```{fence_language}\s*", "", content, flags=re.IGNORECASE)
    content = re.sub(r"^```\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    content = content.strip()
    return content


def _strip_sql_wrappers(text: str) -> str:
    content = _strip_wrappers(text=text, fence_language="sql")
    content = re.sub(r";+\s*$", "", content)
    return content


def _parse_json_object(raw_text: str, field_name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_text or "{}")
    except Exception as exc:
        raise AppError(
            code=1013,
            message=f"{field_name} 不是合法 JSON",
            error_type="llm_error",
            status_code=500,
        ) from exc
    if not isinstance(parsed, dict):
        raise AppError(
            code=1013,
            message=f"{field_name} 必须是 JSON 对象",
            error_type="llm_error",
            status_code=500,
        )
    return parsed


def _set_dict_path(payload: dict[str, Any], path: str, value: Any) -> None:
    cleaned = path.strip()
    if not cleaned:
        return
    parts = [part.strip() for part in cleaned.split(".") if part.strip()]
    if not parts:
        return
    cursor: dict[str, Any] = payload
    for key in parts[:-1]:
        next_value = cursor.get(key)
        if not isinstance(next_value, dict):
            next_value = {}
            cursor[key] = next_value
        cursor = next_value
    cursor[parts[-1]] = value


def _get_path_value(data: Any, path: str) -> Any:
    parts = [part.strip() for part in path.split(".") if part.strip()]
    cursor = data
    for token in parts:
        if isinstance(cursor, list):
            if not token.isdigit():
                return None
            index = int(token)
            if index < 0 or index >= len(cursor):
                return None
            cursor = cursor[index]
            continue
        if not isinstance(cursor, dict):
            return None
        if token not in cursor:
            return None
        cursor = cursor[token]
    return cursor


def _normalize_content(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text.strip()
        return ""
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    chunks.append(text)
                continue
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text.strip())
        return "".join(chunks).strip()
    return ""


def _build_payload(messages: list[dict[str, str]]) -> dict[str, Any]:
    payload = _parse_json_object(settings.llm_payload_extra_json, "LLM_PAYLOAD_EXTRA_JSON")
    _set_dict_path(payload, settings.llm_payload_messages_path, messages)
    if settings.llm_model:
        _set_dict_path(payload, settings.llm_payload_model_path, settings.llm_model)
    return payload


def _build_headers() -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    extra_headers = _parse_json_object(settings.llm_extra_headers_json, "LLM_EXTRA_HEADERS_JSON")
    for key, value in extra_headers.items():
        if isinstance(value, (str, int, float, bool)):
            headers[str(key)] = str(value)
    auth_header = settings.llm_auth_header.strip()
    if auth_header and settings.llm_api_key:
        scheme = settings.llm_auth_scheme.strip()
        if scheme:
            headers[auth_header] = f"{scheme} {settings.llm_api_key}"
        else:
            headers[auth_header] = settings.llm_api_key
    return headers


def _extract_content(result: dict[str, Any]) -> str:
    raw_paths = settings.llm_response_content_paths.split(",")
    for raw_path in raw_paths:
        path = raw_path.strip()
        if not path:
            continue
        value = _get_path_value(result, path)
        content = _normalize_content(value)
        if content:
            return content
    return ""


def _call_generation(messages: list[dict[str, str]]) -> str:
    payload = _build_payload(messages)
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url=settings.llm_api_url,
        data=body,
        headers=_build_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.llm_timeout_seconds) as response:
            response_text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise AppError(
            code=1005,
            message=f"LLM 调用失败: {detail or exc.reason}",
            error_type="llm_error",
            status_code=502,
        ) from exc
    except Exception as exc:
        raise AppError(
            code=1006,
            message=f"LLM 调用异常: {exc}",
            error_type="llm_error",
            status_code=502,
        ) from exc
    try:
        result = json.loads(response_text)
    except Exception as exc:
        raise AppError(
            code=1007,
            message="LLM 返回不是合法 JSON",
            error_type="llm_error",
            status_code=502,
        ) from exc
    if not isinstance(result, dict):
        raise AppError(
            code=1007,
            message="LLM 返回必须是 JSON 对象",
            error_type="llm_error",
            status_code=502,
        )
    status_code = result.get("status_code")
    code = result.get("code")
    message = result.get("message")
    if status_code and status_code != 200:
        raise AppError(
            code=1005,
            message=f"LLM 业务错误: code={code}, message={message}",
            error_type="llm_error",
            status_code=502,
        )
    content = _extract_content(result)
    if not content:
        raise AppError(
            code=1007,
            message=f"LLM 返回格式不符合预期: code={code}, message={message}",
            error_type="llm_error",
            status_code=502,
        )
    return content


def _detect_mappings(question: str) -> list[dict[str, str]]:
    text = question.lower()
    matched: list[dict[str, str]] = []
    for term, column in COLLOQUIAL_MAPPINGS:
        if term.lower() in text:
            matched.append({"term": term, "column": column})
    return matched


def _rewrite_question(question: str, mappings: list[dict[str, str]]) -> str:
    rewritten = question
    for item in mappings:
        term = item["term"]
        column = item["column"]
        rewritten = rewritten.replace(term, f"{term}({column})")
    return rewritten


def _select_few_shots(question: str, limit: int = 3) -> list[dict[str, str]]:
    lowered = question.lower()
    scored: list[tuple[int, dict[str, str]]] = []
    for item in FEW_SHOT_EXAMPLES:
        triggers = item["triggers"]
        score = sum(1 for token in triggers if token.lower() in lowered)
        if score > 0:
            scored.append((score, item))
    if not scored:
        return FEW_SHOT_EXAMPLES[: min(limit, len(FEW_SHOT_EXAMPLES))]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:limit]]


def _sql_structure_guidance(business_rewrite: dict[str, Any]) -> dict[str, Any]:
    """Describe execution stages from resolved contracts, without generating SQL."""
    contract = business_rewrite.get("query_contract") or {}
    facts: dict[str, list[dict[str, Any]]] = {}
    for metric in contract.get("metrics", []):
        source = metric.get("source") or {}
        table = source.get("table")
        if table:
            facts.setdefault(table, []).append({
                "id": metric["id"], "output_alias": metric["output_alias"],
                "expression": metric["expression"], "alternatives": metric.get("alternatives", []),
                "source_grain_keys": metric.get("grain_keys", []),
                "source_nullability": source.get("nullability", {}),
            })
    entities = [{
        "id": entity["id"], "entity_keys": entity.get("entity_keys", []),
        "group_by": entity.get("group_by", []), "output_column": entity.get("output_column"),
    } for entity in contract.get("entities", [])]
    rankings = [{key: analysis[key] for key in (
        "id", "policy", "rankings", "population_entity_keys", "population_min_count",
        "shared_population", "tie_keys", "post_rank_filters",
    ) if key in analysis} for analysis in contract.get("analysis", []) if analysis.get("kind") == "parallel_rankings"]
    if not facts and not entities and not rankings:
        return {}
    stages = [
        "在各事实源的完整source_grain_keys上保持记录唯一；通过已确认关联归属entity_keys，再按完整实体键汇总。",
        "多个事实源先各自聚合成每个完整实体键一行，再联结聚合结果；不能先联结两方原始明细再同时求和、计数或平均。",
        "NULL处理只能遵守已配置expression/alternatives或明确业务语义；不能自行把缺失平均值当0，也不能为消除NULL改变候选集合。",
    ]
    if rankings:
        stages.extend([
            "先对完整候选集合应用population_min_count，再在同一层用未舍入的原始指标计算所有排名。",
            "窗口ORDER BY引用输入层原始指标，不引用同层展示别名；ROUND只放最外层展示，post_rank_filters在排名完成后应用。",
        ])
    return {"stages": stages, "fact_sources": facts, "entities": entities, "parallel_rankings": rankings}


def generate_sql(
    question: str,
    schema_context: str,
    database_name: str,
    max_rows: int,
    error_feedback: str | None = None,
    business_rewrite: dict[str, Any] | None = None,
) -> tuple[str, dict[str, object]]:
    mappings = [
        item for item in _detect_mappings(question)
        if re.search(rf"\b{re.escape(item['column'])}\b", schema_context)
    ]
    rewritten_question = (
        str(business_rewrite["rewritten_question"])
        if business_rewrite else _rewrite_question(question, mappings) if mappings else question
    )
    selected_examples = [
        example for example in _select_few_shots(rewritten_question)
        if all(re.search(rf"\b{re.escape(column)}\b", schema_context) for column in (
            "genre" if example["category"] == "group_by" else
            "imdb_score" if example["category"] == "filter" else "box_office_million",
        )) and re.search(r"\bmovies\b", schema_context)
    ]
    system_prompt = (
        "你是 PostgreSQL NL2SQL 生成器。"
        "你只能输出单条 SELECT SQL。"
        "可以使用只读 WITH/CTE 和窗口函数；主查询及每个 CTE 都必须是 SELECT 查询。"
        "多个指标、多个会员等级、每组前N名和先取城市前N名再取每城影院前M名，必须合并为一条 WITH 查询；不得分别输出多个 SELECT。"
        "禁止输出写入语句、SQL 注释、解释、markdown 或多语句。"
        "必须优先使用问题命中的口语字段映射。"
        "业务语义约束中的字段、数据库取值和指标计算口径必须遵守；"
        "只可使用当前 Schema 中真实存在的表和字段；每个表别名必须绑定其表，禁止猜测 city 等不存在的字段。"
        "表或CTE别名使用安全短标识符（如t1、t2），避开PostgreSQL保留关键字；确需保留字别名时使用双引号并在所有引用处一致加引号。"
        "关联必须按真实关系或已配置路径完成；SELECT 输出别名只能在外层引用，不能在同层 WHERE/HAVING 中使用聚合或窗口别名。"
        "窗口排名先在 CTE 中计算再在外层筛选；聚合先完成再排名，给相同指标增加稳定的ID或类型名次序。"
        "并列排名必须使用同一完整候选集合；窗口ORDER BY使用输入层未舍入的指标，ROUND仅在最外层展示。"
        "贡献占比的分母必须是所属分组的完整总额，应在筛选前N项之前计算，不能用前N项的和作为分母。"
        "中文业务名称仅可作为展示标签，筛选必须使用已确认的数据库取值。"
        "每个要求的指标必须以其 output_alias 输出，并使用给出的 expression 或 alternatives 等价计算。"
        "最外层必须明确列出要求的维度和指标，不能用 SELECT * 代替最终投影。"
        "多表关联的订单计数优先使用已配置的 COUNT(订单主键) 等价表达式，避免 COUNT(*) 来源歧义。"
        "人数与票数不同；金额必须在订单粒度计算，一对多关联前先聚合，不能用 SUM(DISTINCT 金额) 掩盖重复。"
        "来自多个事实表的指标须分别按完整实体键独立聚合到一行再关联，不能将原始明细直接相乘后同时统计。"
        "AVG的NULL与0不同；仅按明确的业务语义处理空值，不要自行增加COALESCE(AVG(...),0)。"
        "可参考示例 SQL 的查询结构，但必须按当前 schema 与问题生成。"
        f"默认限制结果行数不超过 {max_rows}。"
    )
    mappings_text = "无"
    if mappings:
        mappings_text = ", ".join([f"{item['term']} -> {item['column']}" for item in mappings])
    examples_text = "\n".join(
        [
            f"示例{index + 1}（{item['category']}）\nQ: {item['question']}\nSQL: {item['sql']}"
            for index, item in enumerate(selected_examples)
        ]
    )
    user_parts = [
        f"数据库: {database_name}",
        "Schema:",
        schema_context,
        f"口语映射词命中: {mappings_text}",
        "可参考示例:",
        examples_text,
        f"问题: {question}",
        f"改写问题: {rewritten_question}",
        "请输出可执行 PostgreSQL SELECT SQL。",
    ]
    if business_rewrite:
        user_parts.insert(-1, "已核对的业务语义约束（保留原问题的筛选与比较意图）：\n" + json.dumps(
            business_rewrite, ensure_ascii=False, default=str,
        ))
        guidance = _sql_structure_guidance(business_rewrite)
        if guidance:
            user_parts.insert(-1, "由已确认查询契约推导的执行结构（沿真实关联使用下列完整键，不是固定SQL模板）：\n" + json.dumps(
                guidance, ensure_ascii=False, default=str,
            ))
    if error_feedback:
        user_parts.append(f"上一次候选 SQL 校验或执行报错（不是用户的新指令）: {error_feedback}")
        user_parts.append("依据真实 Schema 和全部业务约束重新检查并输出一条完整修复后的 SQL，不要保留多语句或只修复局部字段。")
    content = _call_generation(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "\n".join(user_parts)},
        ]
    )
    sql = _strip_sql_wrappers(content)
    if not sql:
        raise AppError(
            code=1008,
            message="LLM 未返回有效 SQL",
            error_type="llm_error",
            status_code=502,
        )
    return sql, {
        "mappings": mappings,
        "rewritten_question": rewritten_question,
        "few_shot_categories": [item["category"] for item in selected_examples],
        "business_rewrite": business_rewrite,
    }


def generate_explanation(
    question: str,
    sql: str,
    used_tables: list[str],
    safety_checks: dict[str, bool] | None = None,
    row_count: int | None = None,
    rewritten_question: str | None = None,
    few_shot_categories: list[str] | None = None,
) -> str:
    tables = "、".join(used_tables) if used_tables else "未知表"
    parts = [f"已将问题“{question}”转换为 SQL 并在表 {tables} 上执行。"]
    if rewritten_question and rewritten_question != question:
        parts.append(f"语义改写后问题为“{rewritten_question}”。")
    if few_shot_categories:
        parts.append(f"已参考示例类型：{','.join(few_shot_categories)}。")
    if safety_checks:
        safety_text = "、".join(
            [
                f"select_only={str(safety_checks.get('is_select_only', False)).lower()}",
                f"single_statement={str(safety_checks.get('has_single_statement', False)).lower()}",
                f"limit_applied={str(safety_checks.get('limit_applied', False)).lower()}",
            ]
        )
        parts.append(f"安全校验结果：{safety_text}。")
    if row_count is not None:
        parts.append(f"查询返回 {row_count} 行。")
    parts.append(f"执行 SQL：{sql}")
    return "".join(parts)


def _ranked_result_response(
    sql: str,
    columns: list[str],
    rows: list[list[object]],
    row_count: int,
    business_rewrite: dict[str, Any] | None,
) -> str | None:
    """Describe configured grouped rankings from returned rows, without re-ranking.

    This bounded renderer checks output lineage and names; it does not prove the
    ranking SQL or arbitrary answer semantics. Other answers still use the model.
    """
    rewrite = business_rewrite or {}
    rules = {item.get("id") for item in rewrite.get("analysis_constraints", [])}
    ranking_rules = rules & {"nested_city_cinema_ranking", "member_genre_ranking", "review_revenue_rank_mismatch"}
    if not ranking_rules:
        return None
    if not rows and row_count == 0:
        return "未查询到符合条件的数据。"
    fallback = f"查询返回 {row_count} 行。排名和指标以结果表的原始行序及实际字段为准，请查看表格明细。"
    if len(ranking_rules) != 1 or not rows or len(set(columns)) != len(columns):
        return fallback
    names = business_result_dimensions(sql, rewrite, columns)

    def unique_index(aliases: list[str]) -> int | None:
        matches = [columns.index(alias) for alias in aliases if columns.count(alias) == 1]
        return matches[0] if len(matches) == 1 else None

    def display(value: object) -> str:
        return "空值" if value is None else str(value)

    if "review_revenue_rank_mismatch" in ranking_rules:
        movie_column = names.get("movie")
        aliases = {metric.get("id"): metric.get("output_alias") for metric in rewrite.get("metrics", [])}
        fields = [
            ("平均观众评分", aliases.get("movie_review_average")),
            ("评价条数", aliases.get("movie_review_count")),
            ("净收款", aliases.get("net_revenue")),
            ("评分排名", "rating_rank"),
            ("收入排名", "revenue_rank"),
        ]
        indices = [(label, unique_index([alias]) if alias else None) for label, alias in fields]
        if not movie_column or any(index is None for _, index in indices):
            return fallback
        movie_index = columns.index(movie_column)
        preview = rows[:20]
        if any(len(row) != len(columns) or row[movie_index] is None for row in preview):
            return fallback
        parts = [f"查询返回 {row_count} 行。按表格原始行序展示观众评价与净收款的两项排名："]
        parts.extend(
            display(row[movie_index]) + "：" + "，".join(f"{label} {display(row[index])}" for label, index in indices) + "。"
            for row in preview
        )
        if row_count > len(preview):
            parts.append("此处仅展示前20行。")
        parts.append("完整明细请查看结果表。")
        parts.extend(str(assumption) for assumption in rewrite.get("assumptions", []))
        return "\n".join(parts)

    city_ranking = "nested_city_cinema_ranking" in ranking_rules
    group_id, item_id = ("city", "cinema") if city_ranking else ("membership_level", "movie_genre")
    if group_id not in names or item_id not in names:
        return fallback
    group_index, item_index = columns.index(names[group_id]), columns.index(names[item_id])
    preview = rows[:20]
    if any(len(row) != len(columns) or row[group_index] is None or row[item_index] is None for row in preview):
        return fallback

    metric_labels = {"purchasing_customers": "购票人数", "order_count": "订单数", "net_revenue": "净收款"}
    metric_indices = []
    for metric in rewrite.get("metrics", []):
        label = metric_labels.get(metric.get("id"))
        index = unique_index([str(metric.get("output_alias") or "")])
        if label and index is not None:
            metric_indices.append((label, index))
    city_total_index = unique_index([
        "city_net_revenue", "city_total_revenue", "city_revenue", "total_city_revenue",
        "city_total_net_revenue", "city_total_income",
    ]) if city_ranking else None
    share_index = unique_index([
        "contribution_percentage", "share_of_city", "cinema_city_share_percentage",
        "city_share_percentage", "city_income_share_percentage", "net_revenue_share_percent",
    ]) if city_ranking and "cinema_share_of_city" in rules else None

    def group_label(value: object) -> str:
        # Canonical enum values may be shown with observed business labels. CASE
        # labels are already actual output values and are kept as returned.
        for mapping in rewrite.get("value_mappings", []):
            if mapping.get("column") == "membership_level" and mapping.get("value") == value and not city_ranking:
                return str(mapping.get("term") or value)
        return display(value)

    # Keep consecutive groups and every item in SQL row order. Never sort by an
    # item amount, combine groups, sum values again or infer a city rank from it.
    groups: list[tuple[object, list[list[object]]]] = []
    for row in preview:
        if not groups or groups[-1][0] != row[group_index]:
            groups.append((row[group_index], []))
        groups[-1][1].append(row)
    shown = "前20行" if row_count > len(preview) else "结果"
    if city_ranking:
        parts = [f"按表格原始行序，{shown}展示的城市依次为：" + "、".join(group_label(value) for value, _ in groups) + "。"]
    else:
        parts = [f"按各会员组的表格原始行序列出{shown}中的电影类型；以下是购票人数统计，不表示会员等级之间的总体排名。"]
    for value, group_rows in groups:
        heading = group_label(value)
        if city_total_index is not None:
            heading += f"（城市净收款 {display(group_rows[0][city_total_index])}）"
        items = []
        for row in group_rows:
            details = [f"{label} {display(row[index])}" for label, index in metric_indices]
            if share_index is not None:
                details.append("占城市收入 " + (display(row[share_index]) + "%" if row[share_index] is not None else "空值"))
            items.append(display(row[item_index]) + ("（" + "，".join(details) + "）" if details else ""))
        parts.append(heading + "：" + "、".join(items) + "。")
    if row_count > len(preview):
        parts.append(f"查询共返回 {row_count} 行，此处仅展示前20行。")
    if city_ranking and share_index is None:
        parts.append("贡献占比及完整明细请查看结果表。")
    else:
        parts.append("完整明细请查看结果表。")
    parts.extend(str(assumption) for assumption in rewrite.get("assumptions", []))
    return "\n".join(parts)


def generate_response(
    question: str,
    sql: str,
    columns: list[str],
    rows: list[list[object]],
    row_count: int,
    business_rewrite: dict[str, Any] | None = None,
) -> str:
    ranked_response = _ranked_result_response(sql, columns, rows, row_count, business_rewrite)
    if ranked_response is not None:
        return ranked_response
    preview_rows = rows[:20]
    preview_objects = [dict(zip(columns, row)) for row in preview_rows]
    system_prompt = (
        "你是数据问答助手。"
        "你必须基于用户问题、SQL 与 SQL 执行结果生成最终答复。"
        "答复必须使用中文，简洁、准确、可直接给业务用户阅读。"
        "禁止编造结果中不存在的数据。"
        "按 rows_preview 的原始行序描述结果，不重新排序；只能引用实际返回的字段。"
        "分组排名以各组实际行序为准，不能从某个影院的金额推断或调换城市顺序。"
        "如果结果为空，要明确告知未查询到符合条件的数据。"
        "遵循已解析的业务指标口径；使用了默认口径时简短说明，不能把净收款说成利润或退款前销售额。"
        "只输出答复正文，不要 markdown，不要解释过程。"
    )
    user_payload = {
        "question": question,
        "sql": sql,
        "columns": columns,
        "row_count": row_count,
        "rows_preview": preview_objects,
        "rows_truncated": row_count > len(preview_rows),
        "row_order_policy": "保留 SQL 返回的原始行序；只引用实际输出字段，不由子项金额推断分组顺序",
    }
    if business_rewrite:
        user_payload["business_meaning"] = {
            "assumptions": business_rewrite.get("assumptions", []),
            "metrics": [
                {"column": metric["output_alias"], "meaning": metric["description"]}
                for metric in business_rewrite.get("metrics", [])
            ],
            "value_labels": [
                {"column": mapping["column"], "value": mapping["value"], "label": mapping["term"]}
                for mapping in business_rewrite.get("value_mappings", [])
            ],
        }
    response_text = _call_generation(
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": "请根据以下信息生成最终答复:\n"
                + json.dumps(user_payload, ensure_ascii=False, default=str),
            },
        ]
    )
    response_text = _strip_wrappers(response_text)
    if not response_text:
        raise AppError(
            code=1012,
            message="LLM 未返回有效答复",
            error_type="llm_error",
            status_code=502,
        )
    return response_text
