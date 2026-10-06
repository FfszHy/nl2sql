"""Validate declarative chart configuration; model output is never executable code."""

import json
import re
from typing import Any

from app.core.errors import AppError

MAX_CHART_CONFIG_LENGTH = 16_000
CHART_TYPES = {"bar", "line", "pie", "scatter"}
CHART_KEYS = {"version", "type", "title", "category", "series", "orientation"}
SERIES_KEYS = {"field", "name", "axis"}


def _invalid_chart() -> AppError:
    return AppError(
        code=1014,
        message="图表配置未通过安全校验",
        error_type="chart_validation_error",
        status_code=502,
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-standard JSON constant")


def _is_utf8_string(value: Any) -> bool:
    if type(value) is not str:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def validate_chart_config(config: Any, columns: list[str]) -> dict[str, Any]:
    """Return only allowed chart fields with deterministic defaults."""
    if type(config) is not dict or set(config) - CHART_KEYS:
        raise _invalid_chart()
    if type(config.get("version")) is not int or config["version"] != 1:
        raise _invalid_chart()
    chart_type = config.get("type")
    if type(chart_type) is not str or chart_type not in CHART_TYPES:
        raise _invalid_chart()

    category = config.get("category")
    if (
        type(category) is not list
        or not 1 <= len(category) <= 3
        or any(not _is_utf8_string(field) or columns.count(field) != 1 for field in category)
        or len(set(category)) != len(category)
    ):
        raise _invalid_chart()
    series = config.get("series")
    if type(series) is not list or not 1 <= len(series) <= 8:
        raise _invalid_chart()

    normalized_series = []
    for item in series:
        if type(item) is not dict or set(item) - SERIES_KEYS:
            raise _invalid_chart()
        field = item.get("field")
        if not _is_utf8_string(field) or columns.count(field) != 1:
            raise _invalid_chart()
        name = item.get("name", field)
        if not _is_utf8_string(name) or len(name) > 100:
            raise _invalid_chart()
        axis = item.get("axis", "primary")
        if type(axis) is not str or axis not in {"primary", "secondary"}:
            raise _invalid_chart()
        normalized_series.append({"field": field, "name": name, "axis": axis})

    if chart_type in {"pie", "scatter"} and (
        len(category) != 1
        or len(normalized_series) != 1
        or normalized_series[0]["axis"] != "primary"
    ):
        raise _invalid_chart()
    if "orientation" in config and (
        chart_type != "bar"
        or type(config["orientation"]) is not str
        or config["orientation"] not in {"vertical", "horizontal"}
    ):
        raise _invalid_chart()

    normalized: dict[str, Any] = {
        "version": 1,
        "type": chart_type,
        "category": list(category),
        "series": normalized_series,
    }
    if "title" in config:
        if not _is_utf8_string(config["title"]) or len(config["title"]) > 200:
            raise _invalid_chart()
        normalized["title"] = config["title"]
    if chart_type == "bar":
        normalized["orientation"] = config.get("orientation", "vertical")
    return normalized


def parse_chart_config(content: str, columns: list[str]) -> dict[str, Any]:
    """Parse strict JSON, including size and schema checks, without evaluating it."""
    if type(content) is not str or len(content) > MAX_CHART_CONFIG_LENGTH:
        raise _invalid_chart()
    content = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        content = fenced.group(1)
    try:
        config = json.loads(
            content,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError) as exc:
        raise _invalid_chart() from exc
    return validate_chart_config(config, columns)
