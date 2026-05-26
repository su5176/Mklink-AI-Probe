"""MKLink Remote — remote debugging via WebSocket."""

from mklink.remote.server import serve
from mklink.remote.client import connect_remote


def serve_fastapi(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    auth_token: str | None = None,
    device_port: str | None = None,
    axf: str | None = None,
    project_root: str = ".",
    auto_connect: bool = False,
):
    """Start the FastAPI-based remote server (requires mklink[gui] extras)."""
    from mklink.remote.api import create_app, run_server
    app = create_app(auth_token=auth_token, project_root=project_root)
    run_server(
        app, host=host, port=port,
        device_port=device_port, axf=axf,
        project_root=project_root, auto_connect=auto_connect,
    )


__all__ = ["serve", "serve_fastapi", "connect_remote"]
