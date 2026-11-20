"""Fábrica de aplicaciones FastAPI con lo transversal ya resuelto:
logging JSON, correlation_id, métricas, health/readiness y errores de negocio.
"""

import logging
import random
import time
import uuid
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .aws import dynamodb_client
from .config import Settings
from .flags import FlagClient
from .logging import correlation_id_var, setup_logging
from .metrics import HttpMetrics

CORRELATION_HEADER = "X-Correlation-ID"


class BusinessError(Exception):
    """Error de negocio esperado (fondos insuficientes, conector ocupado...).

    Se responde con 4xx y un `code` estable que el orquestador SAGA interpreta.
    """

    def __init__(self, code: str, message: str, status_code: int = 409, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


def create_app(
    service_name: str,
    title: str,
    readiness_tables: list[str] | None = None,
    settings: Settings | None = None,
    lifespan=None,
) -> tuple[FastAPI, Settings, logging.Logger, FlagClient]:
    settings = settings or Settings(service_name=service_name)
    log = setup_logging(settings)
    metrics = HttpMetrics(settings)
    flags = FlagClient(settings)
    app = FastAPI(title=title, version=settings.app_version, lifespan=lifespan)
    app.state.settings = settings
    app.state.metrics = metrics

    @app.middleware("http")
    async def correlation_and_metrics(request: Request, call_next: Callable):
        cid = request.headers.get(CORRELATION_HEADER) or str(uuid.uuid4())
        token = correlation_id_var.set(cid)
        start = time.perf_counter()
        try:
            # Fallo simulado por despliegue: demuestra que el canary detecta errores y hace rollback
            if settings.canary_simulate_errors and request.url.path not in ("/healthz",):
                if random.random() < 0.5:
                    response = JSONResponse(status_code=500, content={"code": "SIMULATED_CANARY_ERROR"})
                    log.error("Error simulado de canary", extra={"event": "CANARY_SIMULATED_ERROR"})
                else:
                    response = await call_next(request)
            else:
                response = await call_next(request)
        except Exception:
            log.exception("Excepción no controlada", extra={"event": "UNHANDLED_EXCEPTION"})
            response = JSONResponse(status_code=500, content={"code": "INTERNAL_ERROR"})
        finally:
            correlation_id_var.reset(token)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        response.headers[CORRELATION_HEADER] = cid
        if request.url.path not in ("/healthz", "/readyz"):
            metrics.record(response.status_code)
            log.info(
                f"{request.method} {request.url.path} -> {response.status_code}",
                extra={
                    "event": "HTTP_REQUEST",
                    "correlation_id": cid,
                    "http_status": response.status_code,
                    "duration_ms": elapsed_ms,
                },
            )
        return response

    @app.exception_handler(BusinessError)
    async def business_error_handler(request: Request, exc: BusinessError):
        log.warning(
            exc.message,
            extra={"event": "BUSINESS_ERROR", "error_code": exc.code, **exc.details},
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message, **exc.details},
        )

    @app.get("/healthz", tags=["ops"])
    async def healthz():
        """Liveness: el proceso responde."""
        return {"status": "ok", "service": service_name, "env": settings.env, "version": settings.app_version}

    @app.get("/readyz", tags=["ops"])
    async def readyz():
        """Readiness: el servicio alcanza SUS tablas en DynamoDB."""
        ddb = dynamodb_client(settings.aws_region, settings.aws_endpoint_url)
        checked = []
        for logical in readiness_tables or []:
            name = settings.table(logical)
            try:
                ddb.describe_table(TableName=name)
                checked.append(name)
            except Exception as exc:
                return JSONResponse(status_code=503, content={"status": "not-ready", "table": name, "error": str(exc)})
        return {"status": "ready", "tables": checked, "revision": settings.revision}

    return app, settings, log, flags
