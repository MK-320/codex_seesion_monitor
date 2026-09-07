import hashlib
import hmac
import json
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, cast, override

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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, SecretStr
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp

from codex_monitor import __version__
from codex_monitor.config import (
    DEFAULT_TRACE_ROOT,
    AppConfig,
    LayoutPreferences,
    load_saved_activity_alert_seconds,
    load_saved_layout,
    load_saved_project_roots,
    load_saved_trace_settings,
    save_saved_activity_alert_seconds,
    save_saved_layout,
    save_saved_project_roots,
    save_saved_trace_settings,
)
from codex_monitor.diagnostic_events import (
    DiagnosticEmitter,
    DiagnosticEvent,
    safe_exception_summary,
)
from codex_monitor.diagnostics import build_diagnostics
from codex_monitor.monitor import Monitor
from codex_monitor.schemas import (
    SessionDetail,
    SessionSummary,
    TracePage,
    TraceSearchPage,
    TraceSearchRequest,
    TraceSearchResult,
    trace_page,
    trace_page_events,
)
from codex_monitor.store import SessionStore
from codex_monitor.trace_store import TraceStore

if TYPE_CHECKING:
    from codex_monitor.models import TraceEvent

STATIC_DIR = Path(__file__).with_name("static")


class _SearchCursorError(ValueError):
    pass


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
    trace_enabled, trace_retention_days = (
        (config.trace_enabled, config.trace_retention_days)
        if config.config_file is None
        else load_saved_trace_settings(
            config.config_file,
            config.trace_enabled,
            config.trace_retention_days,
        )
    )
    trace_store: TraceStore | None = None
    if config.config_file is not None or config.trace_root != DEFAULT_TRACE_ROOT:
        trace_store = TraceStore(
            config.trace_root,
            enabled=trace_enabled,
            retention_days=trace_retention_days,
        )
        session_store.attach_trace_store(trace_store)
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
            "trace": session_store.trace_status(),
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

    async def get_session_traces(  # noqa: PLR0913
        session_key: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        before: Annotated[int | None, Query(ge=0)] = None,
        query: Annotated[str | None, Query(max_length=512)] = None,
        metadata_only: bool = False,
        event_kind: Annotated[str | None, Query(max_length=64)] = None,
        trace_status: Annotated[str | None, Query(max_length=32)] = None,
        tool_name: Annotated[str | None, Query(max_length=256)] = None,
        from_time: Annotated[float | None, Query(ge=0)] = None,
        to_time: Annotated[float | None, Query(ge=0)] = None,
        turn_id: Annotated[str | None, Query(max_length=256)] = None,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
    ) -> TracePage:
        session = session_store.get(session_key)
        if session is None:
            persisted = (
                trace_store.read_trace_events(session_key) if trace_store is not None else []
            )
            if not persisted:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
            events = persisted
            try:
                return trace_page_events(
                    events,
                    limit,
                    before,
                    query,
                    metadata_only,
                    event_kind,
                    trace_status,
                    tool_name,
                    from_time,
                    to_time,
                    turn_id,
                    cursor,
                )
            except ValueError as error:
                raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        try:
            return trace_page(
                session,
                limit,
                before,
                query,
                metadata_only,
                event_kind,
                trace_status,
                tool_name,
                from_time,
                to_time,
                turn_id,
                cursor,
            )
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error

    async def search_traces(payload: TraceSearchRequest) -> TraceSearchPage:
        matches: list[tuple[str, TraceEvent]]
        if trace_store is not None:
            matches = trace_store.search_all_events(
                query=payload.query,
                metadata_only=payload.metadata_only,
                provider=payload.provider,
                turn_id=payload.turn_id,
                event_kind=payload.event_kind,
                status=payload.status,
                tool_name=payload.tool_name,
                from_time=payload.from_time,
                to_time=payload.to_time,
            )
        else:
            matches = []
            for session in session_store.all_sessions():
                if payload.session_key is not None and session.session_key != payload.session_key:
                    continue
                if payload.provider is not None and payload.provider != "codex":
                    continue
                page = trace_page_events(
                    session.trace_events,
                    limit=500,
                    query=payload.query,
                    metadata_only=payload.metadata_only,
                    event_kind=payload.event_kind,
                    status=payload.status,
                    tool_name=payload.tool_name,
                    from_time=payload.from_time,
                    to_time=payload.to_time,
                    turn_id=payload.turn_id,
                )
                selected_ids = {item.trace_id for item in page.events}
                matches.extend(
                    (session.session_key, event)
                    for event in session.trace_events
                    if event.trace_id in selected_ids
                )
        if payload.session_key is not None:
            matches = [item for item in matches if item[0] == payload.session_key]
        fingerprint_source = "|".join(
            f"{session}:{event.sequence}:{event.trace_id}" for session, event in matches
        )
        fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
        start = _decode_search_cursor(payload.cursor, fingerprint) if payload.cursor else 0
        if start < 0 or start > len(matches):
            raise HTTPException(status.HTTP_409_CONFLICT, "cursor is invalid or expired")
        selected = matches[start : start + payload.limit]
        results = tuple(
            TraceSearchResult(
                session_key=session_key,
                event=trace_page_events([event]).events[0],
            )
            for session_key, event in selected
        )
        next_cursor = (
            None
            if start + len(selected) >= len(matches)
            else _encode_search_cursor(start + len(selected), fingerprint)
        )
        return TraceSearchPage(
            results=results,
            total=len(matches),
            next_cursor=next_cursor,
            metadata_only=payload.metadata_only,
            query=payload.query,
            index_state="ready" if trace_store is not None else "unavailable",
        )

    async def get_trace_status() -> dict[str, object]:
        return session_store.trace_status()

    class TraceSettings(BaseModel):
        enabled: bool = True
        retention_days: int | None = None

    async def save_trace_settings(request: Request, payload: TraceSettings) -> dict[str, object]:
        ensure_same_origin(request)
        if payload.retention_days is not None and payload.retention_days <= 0:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "retention_days must be positive",
            )
        nonlocal trace_enabled, trace_retention_days
        if config.config_file is not None:
            await run_sync(
                save_saved_trace_settings,
                config.config_file,
                payload.enabled,
                payload.retention_days,
            )
        trace_enabled = payload.enabled
        trace_retention_days = payload.retention_days
        return session_store.configure_traces(
            enabled=payload.enabled,
            retention_days=payload.retention_days,
        )

    async def clear_trace_storage(request: Request) -> dict[str, object]:
        ensure_same_origin(request)
        return session_store.clear_traces()

    async def pause_trace_backfill(request: Request) -> dict[str, object]:
        ensure_same_origin(request)
        session_store.pause_trace_backfill()
        return session_store.trace_status()

    async def resume_trace_backfill(request: Request) -> dict[str, object]:
        ensure_same_origin(request)
        session_store.resume_trace_backfill()
        return session_store.trace_status()

    async def rebuild_trace_history(request: Request) -> dict[str, object]:
        ensure_same_origin(request)
        _ = session_store.rebuild_trace_history()
        await monitor.rebuild_trace_history()
        return session_store.trace_status()

    async def export_session_traces(session_key: str) -> Response:
        session = session_store.get(session_key)
        session_id = "session" if session is None else str(session.session_id)
        if trace_store is not None:
            stream = trace_store.export_session_stream(session_key, session_id)
            if stream is not None:
                return StreamingResponse(
                    stream,
                    media_type="application/json",
                    headers={
                        "Content-Disposition": f'attachment; filename="trace-{session_id}.json"'
                    },
                )
        payload = session_store.export_traces(session_key)
        if payload is None and trace_store is not None:
            persisted = trace_store.read_all_events(session_key)
            if persisted:
                payload = json.dumps(
                    {
                        "schema_version": 1,
                        "provider": "codex",
                        "session_key": session_key,
                        "events": persisted,
                    },
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8")
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Session or trace storage not found")
        return Response(
            content=payload,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="trace-{session_id}.json"'},
        )

    async def get_trace_event(session_key: str, trace_id: str) -> dict[str, object]:
        session = session_store.get(session_key)
        event = (
            None
            if session is None
            else next((item for item in session.trace_events if item.trace_id == trace_id), None)
        )
        if event is None and trace_store is not None:
            event = next(
                (
                    item
                    for item in trace_store.read_trace_events(session_key)
                    if item.trace_id == trace_id
                ),
                None,
            )
        if event is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Trace event not found")
        view = trace_page_events([event]).events[0]
        return cast("dict[str, object]", view.model_dump())

    async def get_trace_content(
        session_key: str,
        trace_id: str,
        content: Literal["input", "result", "raw"] = "raw",
        offset: Annotated[int, Query(ge=0)] = 0,
        length: Annotated[int, Query(ge=1, le=262_144)] = 262_144,
    ) -> dict[str, object]:
        if trace_store is not None:
            stored_chunk = trace_store.read_content_chunk(
                session_key, trace_id, content, offset, length
            )
            if stored_chunk is not None:
                text_chunk, total_length = stored_chunk
                content_hash = trace_store.content_sha256(session_key, trace_id, content)
                return {
                    "trace_id": trace_id,
                    "content": content,
                    "offset": offset,
                    "length": len(text_chunk),
                    "total_length": total_length,
                    "has_more": offset + len(text_chunk) < total_length,
                    "next_offset": offset + len(text_chunk),
                    "sha256": content_hash
                    or hashlib.sha256(text_chunk.encode("utf-8")).hexdigest(),
                    "value_type": "text",
                    "text": text_chunk,
                }
        session = session_store.get(session_key)
        event = (
            None
            if session is None
            else next((item for item in session.trace_events if item.trace_id == trace_id), None)
        )
        if event is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Trace event not found")
        value = event.raw_payload if content == "raw" else getattr(event, f"{content}_value")
        text_value = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        )
        chunk = text_value[offset : offset + length]
        return {
            "trace_id": trace_id,
            "content": content,
            "offset": offset,
            "length": len(chunk),
            "total_length": len(text_value),
            "has_more": offset + len(chunk) < len(text_value),
            "next_offset": offset + len(chunk),
            "sha256": hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
            "value_type": type(value).__name__,
            "text": chunk,
        }

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
        try:
            await monitor.wait_initial_sync()
            version = session_store.version
            now = time.time()
            initial = session_store.snapshot_event(
                now,
                activity_alert_seconds,
                monitor.data_is_fresh(now),
            )
            await websocket.send_text(initial.model_dump_json())
            context = WebSocketContext(websocket, session_store, config, monitor)
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
    app.add_api_route("/api/traces/status", get_trace_status, methods=["GET"])
    app.add_api_route("/api/traces/settings", save_trace_settings, methods=["PUT"])
    app.add_api_route("/api/traces/clear", clear_trace_storage, methods=["POST"])
    app.add_api_route("/api/traces/backfill/pause", pause_trace_backfill, methods=["POST"])
    app.add_api_route("/api/traces/backfill/resume", resume_trace_backfill, methods=["POST"])
    app.add_api_route("/api/traces/rebuild", rebuild_trace_history, methods=["POST"])
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
    app.add_api_route(
        "/api/sessions/{session_key}/traces",
        get_session_traces,
        methods=["GET"],
        response_model=TracePage,
    )
    app.add_api_route(
        "/api/traces/search",
        search_traces,
        methods=["POST"],
        response_model=TraceSearchPage,
    )
    app.add_api_route(
        "/api/sessions/{session_key}/traces/export",
        export_session_traces,
        methods=["GET"],
    )
    app.add_api_route(
        "/api/sessions/{session_key}/traces/{trace_id}",
        get_trace_event,
        methods=["GET"],
    )
    app.add_api_route(
        "/api/sessions/{session_key}/traces/{trace_id}/content",
        get_trace_content,
        methods=["GET"],
    )
    app.add_api_route("/", dashboard, methods=["GET"])
    app.add_api_websocket_route("/ws", websocket_updates)

    return app


def _encode_search_cursor(offset: int, fingerprint: str) -> str:
    payload = json.dumps(
        {"v": 1, "offset": offset, "fingerprint": fingerprint},
        separators=(",", ":"),
    ).encode("utf-8")
    return urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_search_cursor(cursor: str, fingerprint: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = cast("object", json.loads(urlsafe_b64decode(padded).decode("utf-8")))
        if not isinstance(decoded, dict):
            raise _SearchCursorError
        value = cast("dict[str, object]", decoded)
        if (
            value.get("v") != 1
            or value.get("fingerprint") != fingerprint
            or not isinstance(value.get("offset"), int)
        ):
            raise _SearchCursorError
        offset = value.get("offset")
        if not isinstance(offset, int):
            raise _SearchCursorError
    except (ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(status.HTTP_409_CONFLICT, "cursor is invalid or expired") from None
    else:
        return offset


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
