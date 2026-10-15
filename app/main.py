"""FastAPI wiring; index unavailability is an observable state, not a crash."""
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.config import Settings, load_settings
from app.errors import AppError
from app.providers.base import ChatProvider, EmbeddingProvider
from app.providers.http_adapters import SiliconFlowChat, SiliconFlowEmbedding
from app.providers.mock import MockChat, MockEmbedding
from app.schemas import ErrorResponse, HealthResponse, QueryRequest, QueryResponse
from app.service import QueryService

logger = logging.getLogger("rag")


def providers(settings: Settings, client: httpx.AsyncClient) -> tuple[EmbeddingProvider, ChatProvider]:
    settings.check_live()
    if settings.model_mode == "mock":
        return MockEmbedding(), MockChat()
    return SiliconFlowEmbedding(client, settings), SiliconFlowChat(client, settings)


def get_service(request: Request) -> QueryService:
    if request.app.state.config_error:
        raise request.app.state.config_error
    return request.app.state.service


def create_app(settings: Settings | None = None, *, embedding: EmbeddingProvider | None = None,
               chat: ChatProvider | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.config_error = None
        application.state.service = None
        application.state.mode = "live" if os.getenv("MODEL_MODE") == "live" else "mock"
        client: httpx.AsyncClient | None = None
        try:
            configured = settings if settings is not None else load_settings()
            application.state.mode = configured.model_mode
            configured.check_live()
            client = httpx.AsyncClient(timeout=configured.timeout(), follow_redirects=False)
            application.state.http_client = client
            default_embedding, default_chat = providers(configured, client)
            service = QueryService(configured, embedding or default_embedding, chat or default_chat)
            application.state.service = service
            try:
                service.ready()
            except AppError:
                pass  # healthz exposes the exact current readiness reason.
        except AppError as exc:
            application.state.config_error = exc
        try:
            yield
        finally:
            if client is not None:
                await client.aclose()

    application = FastAPI(title="RAG 技术文档问答演示", version="1.0.0", lifespan=lifespan)

    @application.middleware("http")
    async def request_ids(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.error("request_id=%s stage=internal_error mode=%s error_type=%s",
                         request.state.request_id, application.state.mode, type(exc).__name__)
            error = AppError("INTERNAL_ERROR", "服务内部错误，请凭 request_id 排查", 500)
            response = JSONResponse(error.body(request.state.request_id), status_code=500)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @application.exception_handler(AppError)
    async def business_error(request: Request, exc: AppError):
        logger.warning("request_id=%s stage=error mode=%s error_type=%s",
                       request.state.request_id, application.state.mode, exc.code)
        return JSONResponse(exc.body(request.state.request_id), status_code=exc.status)

    @application.exception_handler(RequestValidationError)
    async def request_error(request: Request, exc: RequestValidationError):
        error = AppError("INVALID_REQUEST", "请求字段、类型或数量不合法", 422)
        return JSONResponse(error.body(request.state.request_id), status_code=422)

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        error = AppError("HTTP_ERROR", "请求路径或方法不可用", exc.status_code)
        return JSONResponse(error.body(request.state.request_id), status_code=exc.status_code, headers=exc.headers)

    @application.get("/healthz", response_model=HealthResponse)
    async def healthz(request: Request) -> HealthResponse:
        try:
            service = get_service(request)
            service.ready()
        except AppError as exc:
            return HealthResponse(mode=application.state.mode, index_ready=False, reason=f"{exc.code}: {exc.message}")
        return HealthResponse(mode=application.state.mode, index_ready=True, reason=None)

    errors = {status: {"model": ErrorResponse, "content": {"application/json": {"example": {
        "error": {"code": code, "message": message, "request_id": "example-request-id"}}}}}
        for status, code, message in [(422, "INVALID_REQUEST", "请求字段非法"),
                                      (503, "INDEX_NOT_READY", "索引缺失"),
                                      (502, "MODEL_RESPONSE_INVALID", "模型响应无效"),
                                      (504, "MODEL_TIMEOUT", "模型接口调用超时")]}

    @application.post("/v1/query", response_model=QueryResponse, responses=errors)
    async def query(body: QueryRequest, request: Request,
                    service: Annotated[QueryService, Depends(get_service)]) -> QueryResponse:
        return (await service.query(body, request.state.request_id)).response

    return application


app = create_app()
