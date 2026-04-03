"""
app.py — Entry point to launch the MTG Binder Scanner GUI.

Usage:
    python app.py
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from gui_pyside import main
from local_server import start_local_server, stop_local_server


def _load_startup_env() -> None:
    """Load .env before any backend startup logic.

    Supports both source runs and packaged EXE runs.
    """
    here = Path(__file__).resolve().parent
    candidates = [
        Path.cwd() / ".env",
        here / ".env",
    ]

    for env_path in candidates:
        if env_path.exists() and env_path.is_file():
            load_dotenv(dotenv_path=env_path, override=False)
            return

    load_dotenv(override=False)

if __name__ == "__main__":
    _load_startup_env()
    handle, startup_error = start_local_server()

    if startup_error:
        os.environ["UNSLOTH_STARTUP_ERROR"] = startup_error
    elif handle is not None:
        os.environ["UNSLOTH_STARTUP_INFO"] = (
            f"Local inference backend [{handle.backend}] ready at {handle.base_url} (via: {handle.command})"
        )

    try:
        main()
    finally:
        stop_local_server(handle)
