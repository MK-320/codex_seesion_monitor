import os
from pathlib import Path
from typing import Annotated, Final

import typer
import uvicorn
from pydantic import SecretStr

from codex_monitor.api import create_app
from codex_monitor.config import DEFAULT_CONFIG_FILE, DEFAULT_SESSION_ROOT, AppConfig
from codex_monitor.sidecar import load_sidecar_settings, run_sidecar

DEFAULT_PROJECT_ROOT: Final = Path.cwd()
SIDECAR_ENV_PREFIX: Final = "CODEX_MONITOR_DESKTOP_"
PORT_ZERO_MESSAGE: Final = "--port 0 requires --sidecar"
DESKTOP_ENV_PRESENT: Final = any(key.startswith(SIDECAR_ENV_PREFIX) for key in os.environ)
app = typer.Typer(add_completion=False, no_args_is_help=not DESKTOP_ENV_PRESENT)


@app.command()
def serve(  # noqa: PLR0913
    project: Annotated[
        list[Path] | None,
        typer.Option("--project", exists=True, file_okay=False, resolve_path=True),
    ] = None,
    session_root: Annotated[
        Path,
        typer.Option("--session-root", exists=True, file_okay=False, resolve_path=True),
    ] = DEFAULT_SESSION_ROOT,
    stuck_seconds: Annotated[
        int,
        typer.Option(min=1, help="长时间无进展提醒阈值, 兼容旧参数名"),
    ] = 300,
    port: Annotated[int, typer.Option(min=0, max=65535)] = 8000,
    run_as_sidecar: Annotated[bool, typer.Option("--sidecar")] = False,
    no_saved_projects: Annotated[bool, typer.Option("--no-saved-projects")] = False,
) -> None:
    is_sidecar = run_as_sidecar or DESKTOP_ENV_PRESENT
    if port == 0 and not is_sidecar:
        raise typer.BadParameter(PORT_ZERO_MESSAGE, param_hint="--port")
    config = AppConfig(
        project_roots=tuple(project or (() if is_sidecar else (DEFAULT_PROJECT_ROOT,))),
        session_root=session_root,
        stuck_seconds=stuck_seconds,
        port=port,
        config_file=DEFAULT_CONFIG_FILE,
        load_saved_projects=not no_saved_projects,
    )
    if is_sidecar:
        settings = load_sidecar_settings(os.environ)
        config = config.model_copy(
            update={
                "port": settings.port,
                "startup_token": SecretStr(settings.startup_token),
                "desktop_protocol_version": settings.protocol_version,
            }
        )
        run_sidecar(config)
        return
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
    )
