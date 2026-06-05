"""Start Clerk without requiring users to type the uvicorn command."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def find_available_port(preferred_port: int) -> int:
    for port in (preferred_port, 8001, 8002, 8003, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((DEFAULT_HOST, port))
            return probe.getsockname()[1]
    return preferred_port


def open_browser_when_ready(url: str) -> None:
    time.sleep(1.25)
    webbrowser.open(url)


def main() -> int:
    os.chdir(ROOT_DIR)
    sys.path.insert(0, str(ROOT_DIR))

    requested_port = int(os.environ.get("CLERK_PORT", DEFAULT_PORT))
    host = os.environ.get("CLERK_HOST", DEFAULT_HOST)
    port = find_available_port(requested_port)
    url = f"http://{host}:{port}"

    os.environ.setdefault("CLERK_FRONTEND_URL", url)

    try:
        import uvicorn
    except ModuleNotFoundError:
        print("Clerk could not start because uvicorn is not installed.")
        print("Install the backend dependencies with: pip install -r backend/requirements.txt")
        return 1

    if port != requested_port:
        print(f"Port {requested_port} is busy, so Clerk is starting on {url}")
    else:
        print(f"Starting Clerk on {url}")

    if os.environ.get("CLERK_NO_BROWSER") == "1":
        print("Browser auto-open disabled. Press Ctrl+C here to stop Clerk.")
    else:
        print("Your browser should open automatically. Press Ctrl+C here to stop Clerk.")
        threading.Thread(target=open_browser_when_ready, args=(url,), daemon=True).start()

    from backend.app.main import app

    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
