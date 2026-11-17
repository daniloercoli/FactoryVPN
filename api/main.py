"""
FactoryVPN - Main API Entry Point
"""

from sqlalchemy.exc import IntegrityError
from fastapi.responses import JSONResponse
import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress
from html import escape
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from api.middleware.logging import LoggingMiddleware
from api.middleware.observation_body import ObservationBodyLimit
from api.routers import auth, sessions, gateways, devices, admin, internal, groups
from api.routers import sso, admin_sso, web_auth, session_diagnostics, telemetry, provisioning
from api.routers import certificate_renewal
from api.routers import enrollment
from api.routers import backups, email_security, gateway_observability, server_status
from api.db.session import init_db
from api.config import settings
import logging

logging.basicConfig(
    level=settings.LOG_LEVEL, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events"""
    # Startup
    logger.info("%s API starting up...", settings.APP_NAME)
    await init_db()
    from api.db.session import async_session_maker
    from api.services.sso_service import maintenance_loop

    from api.services.technical_log_writer import run as log_writer
    from api.services.telemetry_history import maintenance as history_maintenance

    logs = asyncio.create_task(log_writer(async_session_maker))
    history = asyncio.create_task(history_maintenance(async_session_maker))
    task = asyncio.create_task(maintenance_loop(async_session_maker))
    try:
        yield
    finally:
        history.cancel()
        with suppress(asyncio.CancelledError):
            await history
        logs.cancel()
        with suppress(asyncio.CancelledError):
            await logs
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    # Shutdown
    logger.info("%s API shutting down...", settings.APP_NAME)


app = FastAPI(
    title=f"{settings.APP_NAME} API",
    description=f"{settings.APP_NAME} industrial VPN API for remote machine assistance",
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


async def swagger_docs(request: Request):
    # FastAPI's HTML helpers interpolate the title verbatim; the display label
    # remains plain text in OpenAPI and is escaped only for these HTML pages.
    root_path = request.scope.get("root_path", "").rstrip("/")
    redirect_url = app.swagger_ui_oauth2_redirect_url
    return get_swagger_ui_html(
        openapi_url=root_path + app.openapi_url,
        title=escape(app.title + " - Swagger UI"),
        oauth2_redirect_url=root_path + redirect_url if redirect_url else None,
        init_oauth=app.swagger_ui_init_oauth,
        swagger_ui_parameters=app.swagger_ui_parameters,
    )


async def swagger_redirect(request: Request):
    return get_swagger_ui_oauth2_redirect_html()


async def redoc_docs(request: Request):
    root_path = request.scope.get("root_path", "").rstrip("/")
    return get_redoc_html(
        openapi_url=root_path + app.openapi_url,
        title=escape(app.title + " - ReDoc"),
    )


# add_route preserves the default GET/HEAD behavior and excludes docs from OpenAPI.
app.add_route("/docs", swagger_docs, include_in_schema=False)
app.add_route(app.swagger_ui_oauth2_redirect_url, swagger_redirect, include_in_schema=False)
app.add_route("/redoc", redoc_docs, include_in_schema=False)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count", "Retry-After"],
)

# Custom Middleware
app.add_middleware(LoggingMiddleware)
app.add_middleware(ObservationBodyLimit)


@app.middleware("http")
async def private_api_responses(request, call_next):
    from api.services.backup_access import maintenance_active, cleanup_request

    cleanup = request.url.path.startswith("/api/v1/internal/sessions/")
    context = cleanup_request.set(cleanup)
    try:
        if request.url.path.startswith("/api/") and maintenance_active() and not cleanup:
            response = JSONResponse(
                {"detail": "Installation maintenance in progress"}, status_code=503
            )
        else:
            response = await call_next(request)
    finally:
        cleanup_request.reset(context)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


# Include Routers
app.include_router(auth.router, prefix="/api/v1/auth", tags=["Authentication"])
app.include_router(web_auth.router, prefix="/api/v1/web", tags=["Portal authentication"])
app.include_router(sso.router, prefix="/api/v1/auth/sso", tags=["SSO"])
app.include_router(admin_sso.router, prefix="/api/v1/admin", tags=["SSO administration"])
app.include_router(sessions.router, prefix="/api/v1/sessions", tags=["Sessions"])
app.include_router(gateway_observability.router, prefix="/api/v1", tags=["Gateway diagnostics"])
app.include_router(server_status.router, prefix="/api/v1", tags=["Server status"])
app.include_router(
    certificate_renewal.router, prefix="/api/v1/gateways", tags=["Gateway certificates"]
)
app.include_router(gateways.router, prefix="/api/v1/gateways", tags=["Gateways"])
app.include_router(devices.router, prefix="/api/v1/devices", tags=["Devices"])
app.include_router(admin.router, prefix="/api/v1/admin", tags=["Admin"])
app.include_router(groups.router, prefix="/api/v1", tags=["Groups"])
app.include_router(session_diagnostics.router, prefix="/api/v1", tags=["Diagnostics"])
app.include_router(internal.router, prefix="/api/v1/internal", tags=["Worker"])
app.include_router(telemetry.router, prefix="/api/v1", tags=["Telemetry"])
app.include_router(provisioning.router, prefix="/api/v1", tags=["Provisioning"])
app.include_router(enrollment.router, prefix="/api/v1", tags=["Enrollment"])
app.include_router(backups.router, prefix="/api/v1/backups", tags=["Backup and recovery"])
app.include_router(email_security.router, prefix="/api/v1", tags=["Email verification"])


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "version": "2.0.0"}


@app.get("/")
async def root():
    """Root endpoint"""
    return {"name": f"{settings.APP_NAME} API", "version": "2.0.0", "docs": "/docs"}


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request, exc):
    return JSONResponse(
        status_code=409, content={"detail": "Resource conflicts with existing data"}
    )


@app.get("/health/ready")
async def readiness():
    from api.db.session import engine
    from sqlalchemy import text

    try:
        async with asyncio.timeout(3):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(status_code=503, content={"status": "database unavailable"})
    return {"status": "ready"}
