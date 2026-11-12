"""Start the FastAPI app on a free local port in a background thread.

The tests then send real HTTP requests to it, exactly like an external client
would. When the `with` block ends, the server shuts down.
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager

import uvicorn


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def running_server(host: str = "127.0.0.1", port: int | None = None, startup_timeout: float = 180.0):
    port = port or _free_port()
    server = uvicorn.Server(uvicorn.Config("app.main:app", host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + startup_timeout  # first start-up downloads the embedding model
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("The API server crashed during start-up; run `uvicorn app.main:app` to see the error.")
        if time.monotonic() > deadline:
            raise TimeoutError("The API server did not start in time.")
        time.sleep(0.2)
    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)
