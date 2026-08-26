import hmac
import json
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, override

import anyio
from anyio.to_thread import run_sync
from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, SecretStr
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp

from codex_monitor import __version__
from codex_monitor.config import (
    AppConfig,
    LayoutPreferences,
    load_saved_activity_alert_seconds,
    load_saved_layout,
    load_saved_project_roots,
    save_saved_activity_alert_seconds,
    save_saved_layout,
    save_saved_project_roots,
)
from codex_monitor.diagnostic_events import (
    DiagnosticEmitter,
    DiagnosticEvent,
    safe_exception_summary,
)
from codex_monitor.diagnostics import build_diagnostics
from codex_monitor.monitor import Monitor
from codex_monitor.schemas import SessionDetail, SessionSummary
from codex_monitor.store import SessionStore

STATIC_DIR = Path(__file__).with_name("static")
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)
AUTHORIZATION_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
TAURI_ORIGIN = "http://tauri.localhost"
TAURI_DEV_ORIGIN = "http://127.0.0.1:1420"
DESKTOP_ORIGINS = (TAURI_ORIGIN, TAURI_DEV_ORIGIN)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    @override
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable"
            if request.url.path.startswith("/static/assets/")
            else "no-store"
        )
        return response


class StartupTokenMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, startup_token: SecretStr | None) -> None:
        super().__init__(app)
        self._startup_token: SecretStr | None = startup_token

    @override
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        token = self._startup_token
        if token is not None and not _matches_startup_token(
            request.headers.get(AUTHORIZATION_HEADER), token
        ):
            return JSONResponse(
                {"detail": "Startup token required"},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        return await call_next(request)


class DiagnosticEventMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, emitter: DiagnosticEmitter) -> None:
        super().__init__(app)
        self._emitter: DiagnosticEmitter = emitter

    @override
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
            self._emitter.emit(
                DiagnosticEvent(
                    level="debug",
                    component="api",
                    event_code="request_completed",
                    duration_ms=min(
                        86_400_000, max(0, round((time.perf_counter() - started) * 1000))
                    ),
                    network={
                        "protocol": "http",
                        "scope": "loopback",
                        "http_status": response.status_code,
                    },
                )
            )
        except BaseException as error:
            self._emitter.emit(
                DiagnosticEvent(
                    level="error",
                    component="api",
                    event_code="request_failed",
                    duration_ms=min(
                        86_400_000, max(0, round((time.perf_counter() - started) * 1000))
                    ),
                    error_summary=safe_exception_summary(error),
                    network={"protocol": "http", "scope": "loopback", "http_status": 500},
                )
            )
            raise
        return response


@dataclass(frozen=True, slots=True)
class WebSocketContext:
    websocket: WebSocket
    store: SessionStore
    config: AppConfig
    monitor: Monitor


class ProjectImport(BaseModel):
    project_root: str
    persist: bool = False


class ProjectBatchImport(BaseModel):
    project_roots: list[str]
    persist: bool = False


class DiagnosticExport(BaseModel):
    confirmed: Literal[True]


class ActivitySettings(BaseModel):
    activity_alert_seconds: Literal[180, 300, 600, 900] | None


def pick_project_directory() -> Path | None:
    import tkinter as tk  # noqa: PLC0415 - browser-only fallback excluded from sidecar builds
    from tkinter import filedialog  # noqa: PLC0415

    root = tk.Tk()
    root.withdraw()
    try:
        selected = filedialog.askdirectory(parent=root, title="选择要导入的项目文件夹")
    finally:
        root.destroy()
    return Path(selected) if selected else None


def _matches_startup_token(candidate: str | None, expected: SecretStr) -> bool:
    return (
        candidate is not None
        and candidate.startswith(BEARER_PREFIX)
        and hmac.compare_digest(
            candidate.removeprefix(BEARER_PREFIX),
            expected.get_secret_value(),
        )
    )


def _matching_websocket_subprotocol(
    websocket: WebSocket,
    token: SecretStr,
    protocol_version: int,
) -> str | None:
    prefix = f"codex-monitor-v{protocol_version}."
    for protocol in websocket.headers.get("sec-websocket-protocol", "").split(","):
        candidate = protocol.strip()
        if candidate.startswith(prefix) and hmac.compare_digest(
            candidate.removeprefix(prefix), token.get_secret_value()
        ):
            return candidate
    return None


def create_app(  # noqa: C901, PLR0915
    config: AppConfig,
    store: SessionStore | None = None,
    diagnostic_emitter: DiagnosticEmitter | None = None,
) -> FastAPI:
    session_store = SessionStore() if store is None else store
    monitor = Monitor(config, session_store, diagnostic_emitter)
    started_at = time.time()
    websocket_clients = 0
    saved_roots = set(
        () if config.config_file is None else load_saved_project_roots(config.config_file)
    )
    saved_layout = None if config.config_file is None else load_saved_layout(config.config_file)
    activity_alert_seconds = (
        config.stuck_seconds
        if config.config_file is None
        else load_saved_activity_alert_seconds(config.config_file, config.stuck_seconds)
    )
    monitor.set_activity_alert_seconds(activity_alert_seconds)

    def ensure_same_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        allowed_origins = {str(request.base_url).rstrip("/")}
        if config.startup_token is not None:
            allowed_origins.update(DESKTOP_ORIGINS)
        if origin is not None and origin.rstrip("/") not in allowed_origins:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-origin project mutation denied")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        if diagnostic_emitter is not None:
            diagnostic_emitter.emit(
                DiagnosticEvent(level="debug", component="monitor", event_code="monitor_starting")
            )

        async def run_monitor() -> None:
            try:
                await monitor.run()
            except Exception as error:
                if diagnostic_emitter is not None:
                    diagnostic_emitter.emit(
                        DiagnosticEvent(
                            level="error",
                            component="monitor",
                            event_code="monitor_runtime_failed",
                            error_summary=safe_exception_summary(error),
                        )
                    )
                raise

        async with anyio.create_task_group() as task_group:
            monitor_task = task_group.start_soon(run_monitor)
            del monitor_task
            yield
            task_group.cancel_scope.cancel()

    app = FastAPI(
        title="Codex Session Monitor",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(StartupTokenMiddleware, startup_token=config.startup_token)
    app.add_middleware(SecurityHeadersMiddleware)
    if diagnostic_emitter is not None:
        app.add_middleware(DiagnosticEventMiddleware, emitter=diagnostic_emitter)
    if config.startup_token is not None:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(DESKTOP_ORIGINS),
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=[AUTHORIZATION_HEADER, "Content-Type"],
        )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    async def list_sessions() -> tuple[SessionSummary, ...]:
        now = time.time()
        return session_store.summaries(now, activity_alert_seconds, monitor.data_is_fresh(now))

    async def app_config() -> dict[str, object]:
        active_projects = await monitor.projects()
        active_roots = {project.root.resolve() for project in active_projects}
        return {
            "activity_alert_seconds": activity_alert_seconds,
            "layout": None if saved_layout is None else saved_layout.model_dump(),
            "projects": [
                {
                    "project_key": project.key,
                    "project_root": str(project.root),
                    "project_name": project.name,
                    "status": "active" if project.root.is_dir() else "missing",
                    "persisted": project.root.resolve() in saved_roots,
                }
                for project in active_projects
            ]
            + [
                {
                    "project_key": root.name or str(root),
                    "project_root": str(root),
                    "project_name": root.name or str(root),
                    "status": "missing",
                    "persisted": True,
                }
                for root in sorted(saved_roots - active_roots, key=str)
            ],
        }

    async def save_activity_settings(
        request: Request,
        payload: ActivitySettings,
    ) -> Response:
        nonlocal activity_alert_seconds
        ensure_same_origin(request)
        if config.config_file is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Persistence disabled")
        await run_sync(
            save_saved_activity_alert_seconds,
            config.config_file,
            payload.activity_alert_seconds,
        )
        activity_alert_seconds = payload.activity_alert_seconds
        monitor.set_activity_alert_seconds(activity_alert_seconds)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def save_layout_preferences(
        request: Request,
        payload: LayoutPreferences,
    ) -> Response:
        nonlocal saved_layout
        ensure_same_origin(request)
        if config.config_file is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Persistence disabled")
        await run_sync(save_saved_layout, config.config_file, payload)
        saved_layout = payload
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def health() -> dict[str, object]:
        projects = await monitor.projects()
        runtime = monitor.runtime_health()
        return {
            "version": __version__,
            "started_at": started_at,
            "project_count": len(projects),
            **session_store.health_counts(),
            "known_file_count": runtime.known_file_count,
            "last_reconciliation_at": runtime.last_reconciliation_at,
            "reconciliation_count": runtime.reconciliation_count,
            "coalesced_event_count": runtime.coalesced_event_count,
            "websocket_clients": websocket_clients,
            "listen_host": "127.0.0.1",
            "protocol_version": config.desktop_protocol_version,
        }

    async def diagnostic_data() -> dict[str, object]:
        now = time.time()
        runtime = monitor.runtime_health()
        return build_diagnostics(
            version=__version__,
            started_at=started_at,
            project_count=len(await monitor.projects()),
            summaries=session_store.summaries(
                now,
                activity_alert_seconds,
                monitor.data_is_fresh(now),
            ),
            health_counts=session_store.health_counts(),
            monitor_health={
                "known_file_count": runtime.known_file_count,
                "last_reconciliation_at": runtime.last_reconciliation_at,
                "reconciliation_count": runtime.reconciliation_count,
                "coalesced_event_count": runtime.coalesced_event_count,
            },
            websocket_clients=websocket_clients,
            stuck_seconds=activity_alert_seconds,
            now=now,
        )

    async def diagnostics() -> JSONResponse:
        return JSONResponse(
            await diagnostic_data(),
            headers={"Cache-Control": "no-store"},
        )

    async def export_diagnostics(payload: DiagnosticExport) -> Response:
        del payload
        content = json.dumps(await diagnostic_data(), ensure_ascii=False, indent=2)
        return Response(
            content,
            media_type="application/json",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": 'attachment; filename="codex-monitor-diagnostics.json"',
            },
        )

    async def import_project(
        request: Request,
        payload: ProjectImport,
        response: Response,
    ) -> dict[str, str | bool]:
        ensure_same_origin(request)
        try:
            project, added = await monitor.add_project(Path(payload.project_root))
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        response.status_code = status.HTTP_201_CREATED if added else status.HTTP_200_OK
        if payload.persist:
            if config.config_file is None:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Persistence disabled")
            saved_roots.add(project.root.resolve())
            await run_sync(
                save_saved_project_roots,
                config.config_file,
                tuple(sorted(saved_roots, key=str)),
            )
        return {
            "project_key": project.key,
            "project_root": str(project.root),
            "project_name": project.name,
            "status": "active",
            "persisted": project.root.resolve() in saved_roots,
        }

    async def import_projects(
        request: Request,
        payload: ProjectBatchImport,
        response: Response,
    ) -> dict[str, object]:
        ensure_same_origin(request)
        if not payload.project_roots:
            return {"projects": [], "errors": []}
        results, errors = await monitor.add_projects(
            tuple(Path(root) for root in payload.project_roots)
        )
        if not results and errors:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, errors[0][1])
        if payload.persist:
            if config.config_file is None:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Persistence disabled")
            saved_roots.update(project.root.resolve() for project, _ in results)
            await run_sync(
                save_saved_project_roots,
                config.config_file,
                tuple(sorted(saved_roots, key=str)),
            )
        response.status_code = (
            status.HTTP_201_CREATED if any(added for _, added in results) else status.HTTP_200_OK
        )
        return {
            "projects": [
                {
                    "project_key": project.key,
                    "project_root": str(project.root),
                    "project_name": project.name,
                    "status": "active",
                    "persisted": project.root.resolve() in saved_roots,
                }
                for project, _ in results
            ],
            "errors": [{"project_root": str(root), "detail": detail} for root, detail in errors],
        }

    async def pick_and_import_project(request: Request) -> Response:
        ensure_same_origin(request)
        if config.startup_token is not None:
            return JSONResponse(
                {"detail": "Desktop clients must use the native directory picker"},
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
            )
        selected = await run_sync(pick_project_directory)
        if selected is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        try:
            project, added = await monitor.add_project(selected)
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        if config.config_file is not None:
            saved_roots.add(project.root.resolve())
            await run_sync(
                save_saved_project_roots,
                config.config_file,
                tuple(sorted(saved_roots, key=str)),
            )
        return JSONResponse(
            {
                "project_key": project.key,
                "project_root": str(project.root),
                "project_name": project.name,
                "status": "active",
                "persisted": config.config_file is not None,
            },
            status_code=status.HTTP_201_CREATED if added else status.HTTP_200_OK,
        )

    async def remove_project(request: Request, project_key: str) -> Response:
        ensure_same_origin(request)
        project = await monitor.remove_project(project_key)
        root = (
            project.root.resolve()
            if project is not None
            else next(
                (
                    saved_root
                    for saved_root in saved_roots
                    if (saved_root.name or str(saved_root)) == project_key
                ),
                None,
            )
        )
        if root is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
        saved_roots.discard(root)
        if config.config_file is not None:
            await run_sync(
                save_saved_project_roots,
                config.config_file,
                tuple(sorted(saved_roots, key=str)),
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def get_session(
        session_key: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        before: Annotated[int | None, Query(ge=0)] = None,
        anchor_turn_id: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    ) -> SessionDetail:
        if before is not None and anchor_turn_id is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "before and anchor_turn_id are mutually exclusive",
            )
        detail = session_store.detail(
            session_key,
            time.time(),
            activity_alert_seconds,
            limit,
            before,
            anchor_turn_id,
            monitor.data_is_fresh(time.time()),
        )
        if detail is None:
            if anchor_turn_id is not None and session_store.get(session_key) is not None:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    "Attention anchor not found",
                )
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
        return detail

    async def websocket_updates(websocket: WebSocket) -> None:
        nonlocal websocket_clients
        token = config.startup_token
        subprotocol = (
            None
            if token is None
            else _matching_websocket_subprotocol(websocket, token, config.desktop_protocol_version)
        )
        if token is not None and subprotocol is None:
            if diagnostic_emitter is not None:
                diagnostic_emitter.emit(
                    DiagnosticEvent(
                        level="warn",
                        component="websocket",
                        event_code="websocket_auth_rejected",
                        network={"protocol": "websocket", "scope": "auth", "status": 100},
                    )
                )
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        origin = websocket.headers.get("origin")
        if token is None and origin is not None:
            expected_http = f"http://{websocket.headers['host']}"
            expected_https = f"https://{websocket.headers['host']}"
            if origin.rstrip("/") not in {expected_http, expected_https}:
                if diagnostic_emitter is not None:
                    diagnostic_emitter.emit(
                        DiagnosticEvent(
                            level="warn",
                            component="websocket",
                            event_code="websocket_origin_rejected",
                            network={"protocol": "websocket", "scope": "origin", "status": 100},
                        )
                    )
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
        await websocket.accept(subprotocol=subprotocol)
        websocket_clients += 1
        version = session_store.version
        now = time.time()
        initial = session_store.snapshot_event(
            now,
            activity_alert_seconds,
            monitor.data_is_fresh(now),
        )
        await websocket.send_text(initial.model_dump_json())
        context = WebSocketContext(websocket, session_store, config, monitor)
        try:
            async with anyio.create_task_group() as task_group:
                updates_task = task_group.start_soon(_send_updates, context, version)
                del updates_task
                close_message = await websocket.receive_text()
                del close_message
                task_group.cancel_scope.cancel()
        except* WebSocketDisconnect:
            pass
        finally:
            websocket_clients -= 1

    app.add_api_route(
        "/api/sessions",
        list_sessions,
        methods=["GET"],
        response_model=list[SessionSummary],
    )
    app.add_api_route("/api/config", app_config, methods=["GET"])
    app.add_api_route("/api/config/layout", save_layout_preferences, methods=["PUT"])
    app.add_api_route("/api/config/activity", save_activity_settings, methods=["PUT"])
    app.add_api_route("/api/health", health, methods=["GET"])
    app.add_api_route("/api/diagnostics", diagnostics, methods=["GET"])
    app.add_api_route("/api/diagnostics/export", export_diagnostics, methods=["POST"])
    app.add_api_route("/api/projects", import_project, methods=["POST"])
    app.add_api_route("/api/projects/batch", import_projects, methods=["POST"])
    app.add_api_route("/api/projects/pick", pick_and_import_project, methods=["POST"])
    app.add_api_route("/api/projects/{project_key}", remove_project, methods=["DELETE"])
    app.add_api_route(
        "/api/sessions/{session_key}",
        get_session,
        methods=["GET"],
        response_model=SessionDetail,
    )
    app.add_api_route("/", dashboard, methods=["GET"])
    app.add_api_websocket_route("/ws", websocket_updates)

    return app


async def _send_updates(context: WebSocketContext, start_version: int) -> None:
    version = start_version
    while True:
        next_version = await context.store.wait_for_change(version)
        event = context.store.event_after(
            version,
            time.time(),
            context.monitor.activity_alert_seconds,
            context.monitor.data_is_fresh(time.time()),
        )
        await context.websocket.send_text(event.model_dump_json())
        version = next_version
