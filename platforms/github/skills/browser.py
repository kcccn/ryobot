"""Browser inspection skills for frontend runtime diagnostics."""
from __future__ import annotations

import asyncio
import os
import shlex
import socket
import time
from pathlib import Path
from typing import Any

from ._base import GitHubSkillBase
from ._models import BrowserInspectArgs, BrowserStartServerArgs
from ._utils import _command_timeout_seconds, _safe_subprocess_env

_ALLOWED_SERVER_COMMANDS = frozenset({"uvicorn", "python", "python3", "node", "npm", "flask", "gunicorn", "fastapi"})

def _resolve_workspace() -> str:
    return os.getenv("GITHUB_WORKSPACE", ".")


def _inspect_script_path() -> Path:
    return Path(__file__).resolve().parent.parent / "browser" / "inspect_page.py"


class StartDevServer(GitHubSkillBase):
    name = "start_dev_server"
    description = (
        "Start a dev server in the background and wait for the port to be ready. "
        "Use this before calling inspect_frontend so the browser has something to connect to. "
        "The server process will be killed when the workflow ends."
    )
    args_model = BrowserStartServerArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        cmd_parts = shlex.split(args.command)
        if not cmd_parts:
            return "Command is empty."
        executable = os.path.basename(cmd_parts[0])
        if executable not in _ALLOWED_SERVER_COMMANDS:
            return (
                f"Command '{executable}' is not allowed as a dev server. "
                f"Allowed: {', '.join(sorted(_ALLOWED_SERVER_COMMANDS))}"
            )
        workspace = Path(_resolve_workspace())
        target_dir = (workspace / args.cwd).resolve() if args.cwd else workspace.resolve()
        if not target_dir.is_dir():
            return f"Directory not found: {target_dir}"

        timeout = _command_timeout_seconds()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(target_dir),
                env=_safe_subprocess_env(),
            )
        except FileNotFoundError as exc:
            return f"Failed to start server: {exc}"
        except OSError as exc:
            return f"Failed to start server (OS error): {exc}"

        # Wait for port readiness
        deadline = time.monotonic() + min(args.timeout_seconds, timeout)
        ready = False
        last_error = ""
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", args.port), timeout=2.0):
                    ready = True
                    break
            except (ConnectionRefusedError, OSError) as exc:
                last_error = str(exc)
            # Check if server process died early
            if proc.returncode is not None:
                stderr_data = b""
                try:
                    _, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                return (
                    f"Server process exited with code {proc.returncode} before port was ready.\n"
                    f"stderr: {stderr_data.decode('utf-8', errors='replace')[:2000]}"
                )
            await asyncio.sleep(1.0)

        if not ready:
            return f"Server did not start listening on port {args.port} within {args.timeout_seconds}s. Last error: {last_error}"

        return (
            f"Server started (pid={proc.pid}) in {target_dir}, listening on port {args.port}.\n"
            f"Use inspect_frontend to check the running application."
        )


class InspectFrontend(GitHubSkillBase):
    name = "inspect_frontend"
    description = (
        "Inspect a running frontend page using headless Chromium (Playwright). "
        "Collects console logs, DOM state, WebSocket status, network errors, and JS exceptions "
        "as structured text. No multimodal model needed. "
        "Requires Playwright to be installed in the environment. "
        "Use start_dev_server first if the app is not already running."
    )
    args_model = BrowserInspectArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        script = _inspect_script_path()
        if not script.is_file():
            return f"Inspect script not found at {script}"

        timeout = _command_timeout_seconds()

        try:
            proc = await asyncio.create_subprocess_exec(
                "python",
                str(script),
                "--url",
                args.url,
                "--wait-ms",
                str(args.wait_ms),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_safe_subprocess_env(),
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=min(timeout, 60),
            )
        except asyncio.TimeoutError:
            return "Frontend inspection timed out after 60s."
        except FileNotFoundError:
            return "python not found in PATH."
        except OSError as exc:
            return f"Failed to run inspection script: {exc}"

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        parts: list[str] = []
        if stdout:
            parts.append(stdout.rstrip())
        if stderr:
            parts.append(f"--- stderr ---\n{stderr.rstrip()}")
        return "\n\n".join(parts)
