"""Stdio protocol harness: exercise discovery and a representative invocation
across the actual stdio process boundary (AC.1)."""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _child_env() -> dict[str, str]:
    # Force offline state: strip any configured Qobuz credentials.
    env = {k: v for k, v in os.environ.items() if not k.startswith("QOBUZ_")}
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _send(proc: subprocess.Popen, payload: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()


def _recv(proc: subprocess.Popen) -> dict:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    assert line, f"server closed unexpectedly; stderr={proc.stderr.read() if proc.stderr else ''}"
    return json.loads(line)


def test_stdio_discovery_and_tool_invocation():
    proc = subprocess.Popen(
        [sys.executable, "-m", "qobuz_mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(),
        cwd=REPO,
        text=True,
    )
    try:
        # initialize
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            },
        )
        init = _recv(proc)
        assert init["id"] == 1
        assert init["result"]["serverInfo"]["name"] == "qobuz-mcp"
        assert "tools" in init["result"]["capabilities"]

        # notification (no response expected)
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        # discovery
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools_msg = _recv(proc)
        names = {t["name"] for t in tools_msg["result"]["tools"]}
        assert {"server_status", "search_tracks", "list_playlists", "get_playlist"} <= names
        # Write gate: no mutating tools before endpoint validation.
        assert "create_playlist" not in names
        assert "add_tracks_to_playlist" not in names
        assert "remove_tracks_from_playlist" not in names
        assert "delete_playlist" not in names

        # representative read-only invocation, offline
        _send(
            proc,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "server_status", "arguments": {}}},
        )
        call = _recv(proc)
        assert call["id"] == 3
        payload = json.loads(call["result"]["content"][0]["text"])
        assert payload["ok"] is True
        assert payload["credentials_present"] is False
        assert payload["write_tools_exposed"] is False
    finally:
        proc.terminate()
        proc.wait(timeout=10)
