import uuid
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import audit_log, get_logger
from app.core.response import error_response, success_response

app = FastAPI(title=settings.app_name)
logger = get_logger("app.audit.http")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    from app.services.sqlite_store import init_sqlite_store

    init_sqlite_store()


@app.middleware("http")
async def response_envelope(request: Request, call_next):
    trace_id = request.headers.get("x-trace-id") or str(uuid.uuid4())
    request.state.trace_id = trace_id
    started = perf_counter()
    response = await call_next(request)
    elapsed_ms = int((perf_counter() - started) * 1000)
    audit_log(
        logger,
        "http_request",
        trace_id=trace_id,
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        elapsed_ms=elapsed_ms,
    )
    return response


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    trace_id = getattr(request.state, "trace_id", str(uuid.uuid4()))
    audit_log(
        logger,
        "app_error",
        trace_id=trace_id,
        path=request.url.path,
        error_type=exc.error_type,
        error_code=exc.code,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=error_response(
            code=exc.code,
            message=exc.message,
            error_type=exc.error_type,
            trace_id=trace_id,
        ),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    trace_id = getattr(request.state, "trace_id", str(uuid.uuid4()))
    audit_log(
        logger,
        "validation_error",
        trace_id=trace_id,
        path=request.url.path,
        error_type="validation_error",
        error_code=4220,
    )
    return JSONResponse(
        status_code=422,
        content=error_response(
            code=4220,
            message=str(exc),
            error_type="validation_error",
            trace_id=trace_id,
        ),
    )


@app.exception_handler(Exception)
async def fallback_error_handler(request: Request, exc: Exception):
    trace_id = getattr(request.state, "trace_id", str(uuid.uuid4()))
    audit_log(
        logger,
        "internal_error",
        trace_id=trace_id,
        path=request.url.path,
        error_type="internal_error",
        error_code=5000,
    )
    return JSONResponse(
        status_code=500,
        content=error_response(
            code=5000,
            message=f"内部错误: {exc}",
            error_type="internal_error",
            trace_id=trace_id,
        ),
    )


@app.post("/healthz")
async def healthz(request: Request):
    data = {"status": "ok", "service": settings.app_name}
    return success_response(data=data, trace_id=request.state.trace_id)


@app.post("/datasources/test")
async def datasource_test(payload: dict, request: Request):
    from app.models.contracts import DataSourceTestRequest
    from app.services.database import test_connection

    model = DataSourceTestRequest.model_validate(payload)
    data = test_connection(model.datasource)
    return success_response(data=data, trace_id=request.state.trace_id)


@app.post("/datasources")
async def datasource_create(payload: dict, request: Request):
    from app.models.contracts import DataSourceCreateRequest
    from app.services.datasource_registry import create_datasource

    model = DataSourceCreateRequest.model_validate(payload)
    data = create_datasource(model.name, model.datasource)
    return success_response(data=data, trace_id=request.state.trace_id)


@app.get("/datasources/{data_source_id}")
async def datasource_get(data_source_id: str, request: Request):
    from app.services.datasource_registry import get_datasource

    data = get_datasource(data_source_id)
    return success_response(data=data, trace_id=request.state.trace_id)


@app.post("/datasources/{data_source_id}/test")
async def datasource_test_by_id(data_source_id: str, request: Request):
    from app.services.database import test_connection
    from app.services.datasource_registry import get_datasource_config

    datasource = get_datasource_config(data_source_id)
    data = test_connection(datasource)
    return success_response(data=data, trace_id=request.state.trace_id)


@app.post("/datasources/{data_source_id}/schema/refresh")
async def datasource_schema_refresh(data_source_id: str, request: Request):
    from app.services.schema_cache_service import refresh_schema_cache

    data = refresh_schema_cache(data_source_id)
    return success_response(data=data, trace_id=request.state.trace_id)


@app.post("/datasources/{data_source_id}/semantic-index/refresh")
async def datasource_semantic_index_refresh(data_source_id: str, request: Request):
    from app.services.semantic_config_service import refresh_schema_before_config
    from app.services.schema_cache_service import fetch_schema_with_cache
    from app.services.semantic_retrieval_service import refresh_semantic_index

    refresh_schema_before_config(data_source_id)
    _, schema = fetch_schema_with_cache(data_source_id)
    data = refresh_semantic_index(data_source_id, schema)
    return success_response(data=data, trace_id=request.state.trace_id)


@app.get("/datasources/{data_source_id}/semantic-index/status")
async def datasource_semantic_index_status(data_source_id: str, request: Request):
    from app.services.datasource_registry import get_datasource
    from app.services.semantic_retrieval_service import get_semantic_index_status

    get_datasource(data_source_id)
    data = get_semantic_index_status(data_source_id)
    return success_response(data=data, trace_id=request.state.trace_id)


@app.get("/datasources/{data_source_id}/semantic-config/schema")
async def datasource_semantic_config_schema(data_source_id: str, request: Request):
    from app.services.semantic_config_service import get_semantic_config_schema

    data = get_semantic_config_schema(data_source_id)
    return success_response(data=data, trace_id=request.state.trace_id)


@app.post("/datasources/{data_source_id}/semantic-config")
async def datasource_semantic_config_save(data_source_id: str, payload: dict, request: Request):
    from app.models.contracts import SemanticConfigSaveRequest
    from app.services.semantic_config_service import save_semantic_config
    from app.services.schema_cache_service import fetch_schema_with_cache
    from app.services.semantic_retrieval_service import refresh_semantic_index

    model = SemanticConfigSaveRequest.model_validate(payload)
    data = save_semantic_config(data_source_id, model.tables, model.fields)
    _, schema = fetch_schema_with_cache(data_source_id)
    try:
        index_result = refresh_semantic_index(data_source_id, schema)
        data["reindexed"] = True
        data["indexed_tables"] = index_result["indexed_tables"]
        data["vector_updated_at"] = index_result["updated_at"]
        data["reindex_error"] = None
    except Exception as exc:
        data["reindexed"] = False
        data["indexed_tables"] = 0
        data["vector_updated_at"] = None
        data["reindex_error"] = f"{exc}"
    return success_response(data=data, trace_id=request.state.trace_id)


@app.post("/query")
async def query(payload: dict, request: Request):
    from app.models.contracts import QueryRequest
    from app.services.query_service import execute_query

    model = QueryRequest.model_validate(payload)
    data = execute_query(model, trace_id=request.state.trace_id)
    return success_response(data=data, trace_id=request.state.trace_id)


@app.post("/query/explain")
async def query_explain(payload: dict, request: Request):
    from app.models.contracts import QueryExplainRequest
    from app.services.query_service import explain_query

    model = QueryExplainRequest.model_validate(payload)
    data = explain_query(model, trace_id=request.state.trace_id)
    return success_response(data=data, trace_id=request.state.trace_id)
