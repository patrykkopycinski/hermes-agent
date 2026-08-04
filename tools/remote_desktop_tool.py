"""Remote desktop control tool.

Registers the ``remote_desktop`` tool — starts a VNC server inside a terminal
backend (Docker, SSH, local), exposes it via a web-based noVNC client, and
allows the user to take control of the agent environment.

Phase 3 of the demo-workflow feature.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
from typing import Any, Dict, Optional

from tools.registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

REMOTE_DESKTOP_SCHEMA: Dict[str, Any] = {
    "name": "remote_desktop",
    "description": (
        "Start a remote desktop session on the agent's machine or a "
        "terminal backend. Provides a web-based VNC viewer URL that the "
        "user can open in a browser to interact with the desktop — click "
        "through UI flows, test edge cases, and hand control back to the "
        "agent. Supports local (macOS Screen Sharing), Docker, and SSH "
        "backends. Use for live demo handoffs where the user wants to "
        "interact with the running application."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "stop", "status"],
                "description": (
                    "'start' — launch VNC server + web viewer; "
                    "'stop' — tear down session; "
                    "'status' — check if a session is active."
                ),
            },
            "host": {
                "type": "string",
                "description": (
                    "Target host for remote desktop. 'localhost' for the "
                    "agent's own machine (default). Use an SSH host or "
                    "Docker container name for remote sessions."
                ),
            },
            "port": {
                "type": "integer",
                "description": (
                    "VNC display port (default 5900). The web viewer will "
                    "be on port + 1000 (e.g. 6900 for VNC on 5900)."
                ),
            },
            "password": {
                "type": "string",
                "description": "Optional VNC password for the session.",
            },
        },
        "required": ["action"],
    },
}


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

_active_sessions: Dict[str, Dict[str, Any]] = {}
_session_lock = threading.Lock()


def _find_free_port(start: int = 5900, end: int = 5999) -> int:
    """Find a free port in the VNC range."""
    for port in range(start, end):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"No free VNC port in range {start}-{end}")


# ---------------------------------------------------------------------------
# VNC server management
# ---------------------------------------------------------------------------


def _start_vnc_macos(port: int, password: Optional[str] = None) -> Dict[str, Any]:
    """Start VNC on macOS using Screen Sharing LaunchDaemon."""
    # macOS Screen Sharing runs via a LaunchDaemon plist
    plist_path = "/System/Library/LaunchDaemons/com.apple.screensharing.plist"

    try:
        # Enable Screen Sharing via launchctl
        result = subprocess.run(
            ["sudo", "launchctl", "load", "-w", plist_path],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode != 0:
            # Non-sudo fallback: just report instructions
            return {
                "success": True,
                "backend": "macos-screen-sharing",
                "vnc_port": 5900,
                "web_url": None,
                "vnc_url": "vnc://localhost:5900",
                "instructions": (
                    "To enable Screen Sharing: System Settings > General > "
                    "Sharing > Screen Sharing > Enable. Then connect via "
                    "Finder > Go > Connect to Server (Cmd+K): "
                    "vnc://localhost:5900"
                ),
                "process_pid": None,
            }

        vnc_port = 5900  # macOS Screen Sharing always uses 5900

        return {
            "success": True,
            "backend": "macos-screen-sharing",
            "vnc_port": vnc_port,
            "web_url": None,  # no built-in web viewer
            "vnc_url": f"vnc://localhost:{vnc_port}",
            "instructions": (
                "macOS Screen Sharing enabled. Open Finder > Go > Connect to "
                f"Server (Cmd+K) and enter: vnc://localhost:{vnc_port}"
            ),
            "process_pid": None,
        }
    except FileNotFoundError:
        return {
            "success": True,
            "backend": "macos-screen-sharing-manual",
            "vnc_port": 5900,
            "web_url": None,
            "vnc_url": "vnc://localhost:5900",
            "instructions": (
                "Screen Sharing requires manual activation: System Settings "
                "> General > Sharing > Screen Sharing > Enable. Then connect "
                "via vnc://localhost:5900"
            ),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _start_vnc_docker(port: int, password: Optional[str] = None) -> Dict[str, Any]:
    """Start a VNC server in a Docker container with noVNC web client."""
    container_name = "hermes-vnc"
    vnc_password = password or "hermes123"
    web_port = port + 1000

    # Stop existing container
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True,
        text=True,
        timeout=10,
    )

    # Start a container with VNC + noVNC
    # Uses the accetto/ubuntu-vnc-xfx image which bundles noVNC
    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container_name,
                "-p",
                f"{web_port}:5901",
                "-p",
                f"{port}:5900",
                "-e",
                f"VNC_PW={vnc_password}",
                "-e",
                "RESOLUTION=1280x720",
                "accetto/ubuntu-vnc-xfce:latest",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            return {"success": False, "error": f"Docker start failed: {result.stderr}"}

        container_id = result.stdout.strip()[:12]
        time.sleep(3)  # wait for VNC server to be ready

        return {
            "success": True,
            "backend": "docker-novnc",
            "container_id": container_id,
            "vnc_port": port,
            "web_url": f"http://localhost:{web_port}",
            "vnc_url": f"vnc://localhost:{port}",
            "password": vnc_password,
            "instructions": (
                f"Open this URL in your browser: http://localhost:{web_port}\n"
                f"Password: {vnc_password}"
            ),
        }
    except FileNotFoundError:
        return {"success": False, "error": "Docker not installed or not in PATH"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _start_vnc_ssh(
    host: str, port: int, password: Optional[str] = None
) -> Dict[str, Any]:
    """Start an SSH-tunneled VNC session."""
    try:
        # Check SSH connectivity
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", host, "echo", "connected"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {
                "success": False,
                "error": f"SSH connection to {host} failed: {result.stderr}",
            }

        # Create SSH tunnel for VNC port
        # Assumes VNC is already running on the remote host at port 5900
        tunnel_port = port
        tunnel = subprocess.Popen(
            [
                "ssh",
                "-f",
                "-N",
                "-L",
                f"{tunnel_port}:localhost:5900",
                host,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        return {
            "success": True,
            "backend": "ssh-tunnel",
            "host": host,
            "vnc_port": tunnel_port,
            "vnc_url": f"vnc://localhost:{tunnel_port}",
            "instructions": (
                f"SSH tunnel established to {host}.\n"
                f"Connect your VNC viewer to: localhost:{tunnel_port}"
            ),
        }
    except FileNotFoundError:
        return {"success": False, "error": "SSH not available"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


def remote_desktop(
    action: str,
    host: str = "localhost",
    port: int = 5900,
    password: Optional[str] = None,
    **kwargs: Any,
) -> str:
    """Manage a remote desktop session for live demo handoff."""
    session_key = host if host != "localhost" else "local"

    if action == "status":
        with _session_lock:
            session = _active_sessions.get(session_key)
        if session:
            return json.dumps(
                {
                    "active": True,
                    "session": session,
                },
                ensure_ascii=False,
            )
        else:
            return json.dumps({
                "active": False,
                "message": "No active remote desktop session",
            })

    elif action == "start":
        with _session_lock:
            if session_key in _active_sessions:
                existing = _active_sessions[session_key]
                return json.dumps(
                    {
                        "success": True,
                        "message": "Session already active",
                        "session": existing,
                    },
                    ensure_ascii=False,
                )

        # Select backend based on host
        if host == "localhost":
            import platform

            system = platform.system()
            if system == "Darwin":
                result = _start_vnc_macos(port, password)
            else:
                # On Linux, use x11vnc if available
                result = _start_vnc_linux(port, password)
        elif host.startswith("docker:") or host == "docker":
            result = _start_vnc_docker(port, password)
        else:
            result = _start_vnc_ssh(host, port, password)

        if result.get("success"):
            with _session_lock:
                _active_sessions[session_key] = result

        return json.dumps(result, ensure_ascii=False)

    elif action == "stop":
        with _session_lock:
            session = _active_sessions.pop(session_key, None)
        if not session:
            return json.dumps({"success": True, "message": "No session to stop"})

        # Clean up based on backend
        backend = session.get("backend", "")
        if backend == "docker-novnc":
            subprocess.run(
                ["docker", "rm", "-f", "hermes-vnc"],
                capture_output=True,
                timeout=10,
            )
        elif backend in ("macos-screen-sharing", "macos-screen-sharing-manual"):
            subprocess.run(
                [
                    "sudo",
                    "launchctl",
                    "unload",
                    "/System/Library/LaunchDaemons/com.apple.screensharing.plist",
                ],
                capture_output=True,
                timeout=15,
            )

        return json.dumps({"success": True, "message": f"Stopped {backend} session"})

    else:
        return json.dumps({"success": False, "error": f"Unknown action: {action}"})


def _start_vnc_linux(port: int, password: Optional[str] = None) -> Dict[str, Any]:
    """Start VNC on Linux using x11vnc."""
    import shutil

    if not shutil.which("x11vnc"):
        return {
            "success": False,
            "error": "x11vnc not installed. Install with: apt install x11vnc",
        }

    vnc_password = password or "hermes123"
    try:
        proc = subprocess.Popen(
            [
                "x11vnc",
                "-display",
                ":0",
                "-forever",
                "-passwd",
                vnc_password,
                "-rfbport",
                str(port),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(2)

        return {
            "success": True,
            "backend": "x11vnc",
            "vnc_port": port,
            "vnc_url": f"vnc://localhost:{port}",
            "password": vnc_password,
            "instructions": (
                f"x11vnc started on port {port}.\n"
                f"Connect your VNC viewer to: localhost:{port}\n"
                f"Password: {vnc_password}"
            ),
            "process_pid": proc.pid,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------


def _check_remote_desktop_available(**kwargs: Any) -> bool:
    """Check if any VNC backend is available."""
    import platform
    import shutil

    system = platform.system()
    if system == "Darwin":
        plist = "/System/Library/LaunchDaemons/com.apple.screensharing.plist"
        return os.path.exists(plist)
    elif shutil.which("x11vnc"):
        return True
    elif shutil.which("docker"):
        return True
    return False


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="remote_desktop",
    toolset="terminal",
    schema=REMOTE_DESKTOP_SCHEMA,
    handler=lambda args, **kw: remote_desktop(
        action=args.get("action", "status"),
        host=args.get("host", "localhost"),
        port=args.get("port", 5900),
        password=args.get("password"),
    ),
    check_fn=_check_remote_desktop_available,
    emoji="🖥️",
    description=(
        "Start/stop a remote desktop (VNC) session for live demo handoff. "
        "Supports macOS Screen Sharing, Docker with noVNC, and SSH tunnels."
    ),
)
