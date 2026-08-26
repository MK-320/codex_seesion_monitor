import asyncio
import json
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import uvicorn

from codex_monitor.api import create_app
from codex_monitor.config import AppConfig
from codex_monitor.diagnostic_events import (
    DiagnosticEmitter,
    DiagnosticEvent,
    resolve_stdout_stream,
    safe_exception_summary,
)

SIDECAR_PROTOCOL_VERSION: Final = 1
STARTUP_POLL_SECONDS: Final = 0.01
TOKEN_LENGTH: Final = 64


@dataclass(frozen=True, slots=True)
class SidecarSettings:
    port: int
    startup_token: str
    protocol_version: int


def load_sidecar_settings(environment: Mapping[str, str]) -> SidecarSettings:
    try:
        port = int(environment["CODEX_MONITOR_DESKTOP_PORT"])
        token = environment["CODEX_MONITOR_DESKTOP_TOKEN"]
        protocol_version = int(environment["CODEX_MONITOR_DESKTOP_PROTOCOL"])
    except (KeyError, TypeError, ValueError) as error:
        message = "Desktop sidecar settings are invalid"
        raise ValueError(message) from error
    if port != 0 or len(token) != TOKEN_LENGTH:
        message = "Desktop sidecar settings are invalid"
        raise ValueError(message)
    if protocol_version != SIDECAR_PROTOCOL_VERSION:
        message = "Desktop sidecar protocol is unsupported"
        raise ValueError(message)
    return SidecarSettings(
        port=port,
        startup_token=token,
        protocol_version=protocol_version,
    )


def run_sidecar(config: AppConfig) -> None:
    asyncio.run(_serve(config))


async def _serve(config: AppConfig) -> None:
    diagnostics = DiagnosticEmitter()
    try:
        listener = socket.create_server((config.host, config.port), backlog=128)
    except OSError as error:
        diagnostics.emit(
            DiagnosticEvent(
                level="error",
                component="sidecar",
                event_code="listener_bind_failed",
                error_summary=safe_exception_summary(error),
            )
        )
        raise
    listener.setblocking(False)  # noqa: FBT003 - socket API requires a boolean mode
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(config, diagnostic_emitter=diagnostics),
            host=config.host,
            port=config.port,
            access_log=False,
            log_config=None,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started:
        if task.done():
            try:
                _ = await task
            except Exception as error:
                diagnostics.emit(
                    DiagnosticEvent(
                        level="error",
                        component="sidecar",
                        event_code="server_start_failed",
                        error_summary=safe_exception_summary(error),
                    )
                )
                raise
            message = "Sidecar exited before becoming ready"
            raise RuntimeError(message)
        await asyncio.sleep(STARTUP_POLL_SECONDS)
    readiness = json.dumps(
        {
            "event": "ready",
            "port": listener.getsockname()[1],
            "protocol_version": config.desktop_protocol_version,
        }
    )
    stdout = resolve_stdout_stream()
    _ = stdout.write(f"{readiness}\n")
    _ = stdout.flush()
    diagnostics.emit(
        DiagnosticEvent(
            level="debug",
            component="sidecar",
            event_code="server_ready",
            network={"protocol": "http", "scope": "loopback", "status": 200},
        )
    )
    await task
