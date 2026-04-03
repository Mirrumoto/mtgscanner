"""
local_server.py — Manage local OpenAI-compatible vision server lifecycle.

This module allows the desktop app to auto-start a local inference server,
set a dynamic UNSLOTH_BASE_URL, and stop the server at shutdown.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import requests


@dataclass
class LocalServerHandle:
    process: subprocess.Popen | None
    base_url: str
    command: str
    backend: str


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _pick_port(host: str, requested_port: int | None) -> int:
    if requested_port and requested_port > 0:
        return requested_port

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _default_command_candidates() -> list[str]:
    candidates: list[str] = []

    if shutil.which("unsloth"):
        candidates.append("unsloth serve --host {host} --port {port}")

    if importlib.util.find_spec("llama_cpp"):
        candidates.append("python -m llama_cpp.server --host {host} --port {port}")

    if shutil.which("ollama"):
        candidates.append("ollama serve")

    if not candidates:
        candidates = [
            "unsloth serve --host {host} --port {port}",
            "python -m llama_cpp.server --host {host} --port {port}",
            "ollama serve",
        ]

    return candidates


def _normalize_command_text(command: str) -> str:
    text = str(command or "").strip().lower()
    text = text.replace("\\", "/")
    text = re.sub(r'["\']', " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _classify_backend(command: str, dynamic_base_url: str) -> tuple[str, str]:
    normalized = _normalize_command_text(command)

    if not normalized:
        return "custom", dynamic_base_url

    if (
        "ollama serve" in normalized
        or "ollama.exe serve" in normalized
        or (("ollama" in normalized or "ollama.exe" in normalized) and " serve" in f" {normalized}")
    ):
        return "ollama", "http://127.0.0.1:11434/v1"

    if "llama_cpp.server" in normalized:
        return "llama_cpp", dynamic_base_url

    if "unsloth" in normalized:
        return "unsloth", dynamic_base_url

    return "custom", dynamic_base_url


def _known_running_base_urls() -> list[str]:
    return [
        "http://127.0.0.1:8080/v1",
        "http://127.0.0.1:8000/v1",
        "http://127.0.0.1:11434/v1",
    ]


def _is_healthy(base_url: str, timeout_seconds: float = 2.0) -> bool:
    probes = [
        f"{base_url.rstrip('/')}/models",
        f"{base_url.rstrip('/')}/health",
    ]
    for probe in probes:
        try:
            response = requests.get(probe, timeout=timeout_seconds)
            if response.status_code < 500:
                return True
        except Exception:
            continue
    return False


def _start_process(command: str) -> subprocess.Popen:
    creation_flags = 0
    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    return subprocess.Popen(
        command,
        shell=True,
        cwd=str(Path.cwd()),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )


def start_local_server() -> tuple[LocalServerHandle | None, str | None]:
    """
    Start local inference server (if enabled) and set UNSLOTH_BASE_URL.

    Returns:
      (handle, error_message)
    """
    if not _env_flag("UNSLOTH_AUTOSTART", True):
        return None, None

    existing_base_url = str(os.environ.get("UNSLOTH_BASE_URL") or "").strip()
    if existing_base_url:
        if not _env_flag("UNSLOTH_AUTOSTART_OVERRIDE_BASE_URL", False):
            return LocalServerHandle(
                process=None,
                base_url=existing_base_url,
                command="(preconfigured)",
                backend="preconfigured",
            ), None
        if _is_healthy(existing_base_url):
            return LocalServerHandle(
                process=None,
                base_url=existing_base_url,
                command="(already running)",
                backend="existing",
            ), None

    for running_url in _known_running_base_urls():
        if _is_healthy(running_url):
            os.environ["UNSLOTH_BASE_URL"] = running_url
            os.environ.setdefault("UNSLOTH_API_KEY", "unsloth-local")
            return LocalServerHandle(
                process=None,
                base_url=running_url,
                command="(auto-detected running server)",
                backend="autodetect",
            ), None

    host = str(os.environ.get("UNSLOTH_SERVER_HOST") or "127.0.0.1").strip()
    requested_port_raw = str(os.environ.get("UNSLOTH_SERVER_PORT") or "").strip()

    requested_port: int | None = None
    if requested_port_raw:
        try:
            requested_port = int(requested_port_raw)
        except ValueError:
            return None, f"Invalid UNSLOTH_SERVER_PORT: {requested_port_raw}"

    port = _pick_port(host, requested_port)
    dynamic_base_url = f"http://{host}:{port}/v1"

    command_template = str(os.environ.get("UNSLOTH_SERVER_COMMAND") or "").strip()
    candidates = [command_template] if command_template else _default_command_candidates()

    timeout_seconds_raw = str(os.environ.get("UNSLOTH_SERVER_START_TIMEOUT") or "45").strip()
    try:
        start_timeout = max(3.0, float(timeout_seconds_raw))
    except ValueError:
        start_timeout = 45.0

    poll_interval = 0.4
    failures: list[str] = []

    for template in candidates:
        if not template:
            continue

        command = template.format(host=host, port=port)
        backend, base_url = _classify_backend(command, dynamic_base_url)

        process = _start_process(command)

        deadline = time.time() + start_timeout
        while time.time() < deadline:
            if process.poll() is not None:
                failures.append(
                    f"{backend} command exited before becoming healthy: {command} "
                    f"(exit code {process.returncode})"
                )
                break
            if _is_healthy(base_url):
                os.environ["UNSLOTH_BASE_URL"] = base_url
                os.environ.setdefault("UNSLOTH_API_KEY", "unsloth-local")
                return LocalServerHandle(
                    process=process,
                    base_url=base_url,
                    command=command,
                    backend=backend,
                ), None
            time.sleep(poll_interval)

        if process.poll() is None:
            failures.append(
                f"{backend} command did not become healthy at {base_url} within {start_timeout:.0f}s: {command}"
            )

        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    command_hint = command_template or " | ".join(_default_command_candidates())
    details = f" Last failure: {failures[-1]}" if failures else ""
    return None, (
        "Could not start local inference server. "
        "Install and run one supported backend (Unsloth, llama-cpp-python, or Ollama), "
        f"or set UNSLOTH_SERVER_COMMAND. Tried: {command_hint}.{details}"
    )


def stop_local_server(handle: LocalServerHandle | None) -> None:
    if handle is None:
        return

    process = handle.process
    if process is None:
        return

    if process.poll() is not None:
        return

    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
