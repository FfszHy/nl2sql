from typing import Any


def success_response(data: dict[str, Any], trace_id: str) -> dict[str, Any]:
    return {
        "code": 0,
        "message": "ok",
        "data": data,
        "trace_id": trace_id,
    }


def error_response(
    code: int,
    message: str,
    error_type: str,
    trace_id: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "error_type": error_type,
        "trace_id": trace_id,
    }
