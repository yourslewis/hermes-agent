from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path


def _can_connect(host: str = "127.0.0.1", port: int = 4000, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_anthropic_shim() -> bool:
    """Ensure the local Claude-Code→LiteLLM Anthropic shim is listening.

    Claude Code reads ~/.claude/settings.json and talks to ANTHROPIC_BASE_URL,
    which we set to http://127.0.0.1:4000. The shim forwards /v1/messages to
    the working local proxy on 4040. Starting it here makes Slack /hrun robust
    after gateway restarts.
    """
    if _can_connect():
        return True
    script = Path.home() / ".hermes" / "profiles" / "chloe" / "scripts" / "anthropic_4040_shim.py"
    if not script.exists():
        return False
    subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Give it a short moment to bind.
    for _ in range(20):
        if _can_connect(timeout=0.2):
            return True
        import time
        time.sleep(0.1)
    return _can_connect()
