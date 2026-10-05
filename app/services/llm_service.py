import json
import re
import urllib.error
import urllib.request
from typing import Any

from app.core.config import settings
from app.core.errors import AppError

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


def generate_sql(
    question: str,
    schema_context: str,
    database_name: str,
    max_rows: int,
    error_feedback: str | None = None,
) -> tuple[str, dict[str, object]]:
    mappings = _detect_mappings(question)
    rewritten_question = _rewrite_question(question, mappings) if mappings else question
    selected_examples = _select_few_shots(rewritten_question)
    system_prompt = (
        "你是 PostgreSQL NL2SQL 生成器。"
        "你只能输出单条 SELECT SQL。"
        "禁止输出解释、禁止 markdown、禁止多语句。"
        "必须优先使用问题命中的口语字段映射。"
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
    if error_feedback:
        user_parts.append(f"上一次 SQL 执行报错: {error_feedback}")
        user_parts.append("请仅输出修复后的 SQL。")
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


def generate_response(
    question: str,
    sql: str,
    columns: list[str],
    rows: list[list[object]],
    row_count: int,
) -> str:
    preview_rows = rows[:20]
    preview_objects = [dict(zip(columns, row)) for row in preview_rows]
    system_prompt = (
        "你是数据问答助手。"
        "你必须基于用户问题、SQL 与 SQL 执行结果生成最终答复。"
        "答复必须使用中文，简洁、准确、可直接给业务用户阅读。"
        "禁止编造结果中不存在的数据。"
        "如果结果为空，要明确告知未查询到符合条件的数据。"
        "只输出答复正文，不要 markdown，不要解释过程。"
    )
    user_payload = {
        "question": question,
        "sql": sql,
        "columns": columns,
        "row_count": row_count,
        "rows_preview": preview_objects,
        "rows_truncated": row_count > len(preview_rows),
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


def generate_echarts_code(
    question: str,
    sql: str,
    columns: list[str],
    row_count: int,
    explanation: str | None,
) -> str:
    system_prompt = (
        "你是数据可视化助手。"
        "你必须根据给定问题和查询结果结构，返回最合适的 ECharts option JavaScript 对象代码。注意：将图例放到右上角，避免与图表重叠。"
        "只输出代码，不要解释，不要 markdown。"
        "必须可直接被前端使用。"
        "不要把具体数据写死到 option。"
        "默认前端会传入变量 columns 和 rows。"
        "请在 option 中直接使用 rows 和 columns。"
    )
    user_payload = {
        "question": question,
        "sql": sql,
        "columns": columns,
        "row_count": row_count,
        "explanation": explanation or "",
    }
    content = _call_generation(
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": "请基于以下数据生成 ECharts option 代码:\n"
                + json.dumps(user_payload, ensure_ascii=False),
            },
        ]
    )
    echarts_code = _strip_wrappers(content, fence_language="javascript")
    if not echarts_code:
        raise AppError(
            code=1011,
            message="LLM 未返回有效图表代码",
            error_type="llm_error",
            status_code=502,
        )
    return echarts_code
