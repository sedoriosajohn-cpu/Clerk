"""
Start Clerk locally — no terminal knowledge required.

First run:  automatically installs Python dependencies.
Every run:  starts the backend and opens your browser.
Stop:       press Ctrl+C in this window.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
REQUIREMENTS = ROOT_DIR / "backend" / "requirements.txt"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# ── Colours (skip on Windows cmd that doesn't support ANSI) ──────────────────
_USE_COLOUR = sys.platform != "win32" or os.environ.get("TERM") == "xterm"
def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text
green  = lambda t: _c("32", t)
yellow = lambda t: _c("33", t)
cyan   = lambda t: _c("36", t)
red    = lambda t: _c("31", t)
bold   = lambda t: _c("1",  t)


def find_available_port(preferred: int) -> int:
    for port in (preferred, 8001, 8002, 8003):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((DEFAULT_HOST, port))
                return port
            except OSError:
                continue
    return preferred


def pip_install() -> bool:
    """Install requirements.txt into the current Python environment."""
    print(cyan("📦  Installing dependencies (first-time setup, ~30 seconds) …"))
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS),
         "--quiet", "--disable-pip-version-check"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(red("❌  pip install failed:"))
        print(result.stderr[-3000:])
        return False
    print(green("✅  Dependencies installed."))
    return True


def ensure_dependencies() -> bool:
    """Return True when all required packages are importable."""
    try:
        import uvicorn          # noqa: F401
        import fastapi          # noqa: F401
        import sqlalchemy       # noqa: F401
        import openai           # noqa: F401
        return True
    except ModuleNotFoundError:
        pass

    if not REQUIREMENTS.exists():
        print(red(f"❌  Could not find {REQUIREMENTS}"))
        print("    Make sure you cloned the full Clerk repository.")
        return False

    ok = pip_install()
    if not ok:
        print()
        print(yellow("Tip: if pip failed, try running this in your terminal:"))
        print(f"    pip install -r {REQUIREMENTS}")
    return ok


def open_browser_when_ready(url: str, timeout: float = 15.0) -> None:
    """Poll until the server responds, then open the browser."""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    webbrowser.open(url)


def check_env() -> None:
    """Warn about common missing configuration; create a starter .env if absent."""
    env_path = ROOT_DIR / ".env"
    if not env_path.exists():
        env_path.write_text(
            "# Clerk local configuration\n"
            "#\n"
            "# Clerk runs without any of these set — you can add tasks manually right away.\n"
            "# Uncomment and fill in the keys below to unlock additional features.\n"
            "#\n"
            "# --- AI extraction (smart task parsing, voice notes, image schedules) ---\n"
            "# OPENAI_API_KEY=sk-...\n"
            "# OPENAI_MODEL=gpt-4o-mini\n"
            "#\n"
            "# --- Database (leave blank to use local SQLite — fine for personal use) ---\n"
            "# DATABASE_URL=postgresql://user:pass@host/dbname\n"
            "#\n"
            "# --- Email (password reset emails; app works without this) ---\n"
            "# SMTP_HOST=smtp.gmail.com\n"
            "# SMTP_PORT=587\n"
            "# SMTP_USERNAME=you@gmail.com\n"
            "# SMTP_PASSWORD=your-app-password\n"
            "# SMTP_FROM=you@gmail.com\n",
            encoding="utf-8",
        )

    # Print feature availability summary
    from dotenv import dotenv_values
    env = dotenv_values(env_path)

    has_openai  = bool(env.get("OPENAI_API_KEY"))
    has_google  = (ROOT_DIR / "credentials.json").exists() or bool(env.get("GOOGLE_CREDENTIALS_JSON"))
    has_smtp    = bool(env.get("SMTP_HOST"))
    has_db      = bool(env.get("DATABASE_URL"))

    print()
    print(bold("  Feature availability:"))
    print(f"    {'✅' if True      else '❌'}  Task manager (manual entry, basic matching) — always on")
    print(f"    {'✅' if has_openai else '⬜'}  AI extraction, voice notes, image schedules  {'(OPENAI_API_KEY set)' if has_openai else '— add OPENAI_API_KEY to .env'}")
    print(f"    {'✅' if has_google else '⬜'}  Gmail / Calendar / Classroom sync            {'(credentials.json found)' if has_google else '— add credentials.json'}")
    print(f"    {'✅' if has_smtp   else '⬜'}  Password-reset emails                        {'(SMTP configured)' if has_smtp else '— add SMTP settings to .env'}")
    print(f"    {'✅' if has_db     else '⬜'}  PostgreSQL database                          {'(DATABASE_URL set)' if has_db else '— using local SQLite (default)'}")
    print()


def main() -> int:
    os.chdir(ROOT_DIR)
    sys.path.insert(0, str(ROOT_DIR))

    print()
    print(bold("  ╔══════════════════════════════╗"))
    print(bold("  ║         Clerk  🗂️             ║"))
    print(bold("  ╚══════════════════════════════╝"))
    print()

    # 1. Dependencies
    if not ensure_dependencies():
        input("\nPress Enter to exit …")
        return 1

    # 2. Env check
    check_env()

    # 3. Load .env
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT_DIR / ".env")
    except ImportError:
        pass

    # 4. Pick port
    requested_port = int(os.environ.get("CLERK_PORT", DEFAULT_PORT))
    host = os.environ.get("CLERK_HOST", DEFAULT_HOST)
    port = find_available_port(requested_port)
    url  = f"http://{host}:{port}"

    if port != requested_port:
        print(yellow(f"⚠️   Port {requested_port} is busy — using {port} instead."))

    print(green(f"\n🚀  Starting Clerk at {bold(url)}"))
    print("    Press Ctrl+C to stop.\n")

    # 5. Open browser once the server is up
    if os.environ.get("CLERK_NO_BROWSER") != "1":
        threading.Thread(target=open_browser_when_ready, args=(url,), daemon=True).start()

    # 6. Run uvicorn
    try:
        import uvicorn
    except ModuleNotFoundError:
        print(red("❌  uvicorn is not installed even after dependency install."))
        print(f"    Try:  pip install -r {REQUIREMENTS}")
        return 1

    uvicorn.run("backend.app.main:app", host=host, port=port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
