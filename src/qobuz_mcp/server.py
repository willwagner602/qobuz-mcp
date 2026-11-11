"""Stdio MCP server for qobuz-mcp.

Implements the MCP stdio surface (newline-delimited JSON-RPC 2.0):
initialize, tools/list, tools/call. Runs fully offline until configured
with eligible credentials; all account operations fail closed without them.

Write tools (create/add/remove/delete playlist) are registered ONLY when
QobuzClient.validation_passed is True, which requires out-of-band endpoint,
auth, and payload validation with eligible Qobuz-issued credentials.
"""

from __future__ import annotations

import json
import sys
from typing import Any, IO

from . import __version__
from .config import config_status, load_client, redact
from .qobuz_api import QobuzClient

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "qobuz-mcp"

# Tools that touch the Qobuz API. Registered always; calls fail closed
# without an eligible configured client.
READ_TOOLS = ("server_status", "search_tracks", "list_playlists", "get_playlist")

# Tools that mutate account state. Registered ONLY after validation_passed.
WRITE_TOOLS = (
    "create_playlist",
    "add_tracks_to_playlist",
    "remove_tracks_from_playlist",
    "delete_playlist",
)


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def tool_definitions(client: QobuzClient | None) -> list[dict[str, Any]]:
    """Tool descriptors advertised over MCP, reflecting the write gate."""
    validation_passed = bool(client is not None and getattr(client, "validation_passed", False))
    tools = [
        {
            "name": "server_status",
            "description": (
                "Report local server state: configured credential presence, "
                "credential eligibility policy, and whether write tools are exposed. "
                "Offline; makes no network requests."
            ),
            "inputSchema": _schema({}, []),
        },
        {
            "name": "search_tracks",
            "description": "Search the Qobuz catalog for tracks. Requires configured credentials.",
            "inputSchema": _schema(
                {
                    "query": {"type": "string", "description": "Free-text search query"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                ["query"],
            ),
        },
        {
            "name": "list_playlists",
            "description": (
                "List the authenticated user's playlists. Requires configured credentials."
            ),
            "inputSchema": _schema(
                {"limit": {"type": "integer", "minimum": 1, "maximum": 50}}, []
            ),
        },
        {
            "name": "get_playlist",
            "description": (
                "Fetch one playlist with its tracks, including the playlist-track ids "
                "needed for removal. Requires configured credentials."
            ),
            "inputSchema": _schema(
                {"playlist_id": {"type": ["integer", "string"]}}, ["playlist_id"]
            ),
        },
    ]
    if validation_passed:
        tools.extend(
            [
                {
                    "name": "create_playlist",
                    "description": "Create a playlist. Requires confirm=true.",
                    "inputSchema": _schema(
                        {
                            "name": {"type": "string"},
                            "is_public": {"type": "boolean"},
                            "confirm": {"type": "boolean"},
                        },
                        ["name"],
                    ),
                },
                {
                    "name": "add_tracks_to_playlist",
                    "description": "Add resolved track ids to a playlist. Requires confirm=true.",
                    "inputSchema": _schema(
                        {
                            "playlist_id": {"type": ["integer", "string"]},
                            "track_ids": {
                                "type": "array",
                                "items": {"type": ["integer", "string"]},
                            },
                            "confirm": {"type": "boolean"},
                        },
                        ["playlist_id", "track_ids"],
                    ),
                },
                {
                    "name": "remove_tracks_from_playlist",
                    "description": (
                        "Remove specific playlist-track ids from an owned playlist. "
                        "Fetches the playlist fresh and verifies ownership first. "
                        "Requires confirm=true."
                    ),
                    "inputSchema": _schema(
                        {
                            "playlist_id": {"type": ["integer", "string"]},
                            "playlist_track_ids": {
                                "type": "array",
                                "items": {"type": ["integer", "string"]},
                            },
                            "confirm": {"type": "boolean"},
                        },
                        ["playlist_id", "playlist_track_ids"],
                    ),
                },
                {
                    "name": "delete_playlist",
                    "description": (
                        "Delete an owned playlist after a fresh ownership check. "
                        "Requires confirm=true."
                    ),
                    "inputSchema": _schema(
                        {
                            "playlist_id": {"type": ["integer", "string"]},
                            "confirm": {"type": "boolean"},
                        },
                        ["playlist_id"],
                    ),
                },
            ]
        )
    return tools


def _text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _require_client(client: QobuzClient | None) -> QobuzClient:
    if client is None:
        raise RuntimeError(
            "Credentials required: set QOBUZ_APP_ID and QOBUZ_USER_AUTH_TOKEN "
            "(manual web-session route; see README). For remove/delete, also "
            "set QOBUZ_USER_ID so ownership can be verified."
        )
    return client


def _strict_confirm(value: Any) -> bool:
    """Confirmation must be the exact JSON boolean true; "true", 1, etc. don't count."""
    return value is True


def call_tool(client: QobuzClient | None, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tools/call. Account operations fail closed without a client."""
    if name == "server_status":
        status = config_status()
        payload = {
            "ok": True,
            "credentials_present": status.credentials_present,
            "credential_policy": (
                "Manual web-session route (operator-authorized): app_id and user "
                "auth token copied by hand from your own play.qobuz.com session; "
                "passwords are never handled and extraction is never automated. "
                "Writes additionally require QOBUZ_ENABLE_WRITES=1 after a "
                "successful read-only smoke test."
            ),
            "write_tools_exposed": bool(
                client is not None and getattr(client, "validation_passed", False)
            ),
            "api_base_note": (
                "Endpoints are observed and undocumented; unverified against live service."
            ),
        }
        return _text_result(json.dumps(payload, indent=2))

    if name == "search_tracks":
        c = _require_client(client)
        results = c.search_tracks(
            query=str(arguments["query"]), limit=int(arguments.get("limit", 20))
        )
        return _text_result(json.dumps({"tracks": results}, indent=2))

    if name == "list_playlists":
        c = _require_client(client)
        playlists = c.get_user_playlists(limit=int(arguments.get("limit", 50)))
        return _text_result(json.dumps({"playlists": playlists}, indent=2))

    if name == "get_playlist":
        c = _require_client(client)
        playlist = c.get_playlist(arguments["playlist_id"])
        return _text_result(json.dumps(playlist, indent=2))

    if name in WRITE_TOOLS:
        c = _require_client(client)
        if not getattr(c, "validation_passed", False):
            raise RuntimeError(
                "Write tools are not enabled: set QOBUZ_ENABLE_WRITES=1 "
                "(after the read-only smoke test) to expose playlist writes"
            )
        if name == "create_playlist":
            created = c.create_playlist(
                name=str(arguments["name"]),
                confirm=_strict_confirm(arguments.get("confirm", False)),
                is_public=bool(arguments.get("is_public", False)),
            )
            return _text_result(json.dumps({"created": created}, indent=2))
        if name == "add_tracks_to_playlist":
            result = c.add_tracks(
                arguments["playlist_id"],
                list(arguments["track_ids"]),
                confirm=_strict_confirm(arguments.get("confirm", False)),
            )
            return _text_result(json.dumps({"added": result}, indent=2))
        if name == "remove_tracks_from_playlist":
            result = c.remove_tracks(
                arguments["playlist_id"],
                list(arguments["playlist_track_ids"]),
                confirm=_strict_confirm(arguments.get("confirm", False)),
            )
            return _text_result(json.dumps({"removed": result}, indent=2))
        if name == "delete_playlist":
            result = c.delete_playlist(
                arguments["playlist_id"],
                confirm=_strict_confirm(arguments.get("confirm", False)),
            )
            return _text_result(json.dumps({"deleted": result}, indent=2))
    raise KeyError(f"Unknown tool: {name}")


def handle_request(
    client: QobuzClient | None, message: dict[str, Any]
) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Returns a response dict, or None for notifications."""
    if not isinstance(message, dict) or not isinstance(message.get("method"), str):
        return {
            "jsonrpc": "2.0",
            "id": message.get("id") if isinstance(message, dict) else None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
    method = message.get("method")
    msg_id = message.get("id")
    is_notification = msg_id is None

    def reply(result: dict[str, Any]) -> dict[str, Any] | None:
        return None if is_notification else {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def reply_error(code: int, err_text: str) -> dict[str, Any] | None:
        if is_notification:
            return None
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": redact(err_text, _secret_values(client))},
        }

    try:
        if method == "initialize":
            requested = (message.get("params") or {}).get("protocolVersion", PROTOCOL_VERSION)
            return reply(
                {
                    "protocolVersion": requested,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": __version__},
                }
            )
        if method in ("notifications/initialized", "initialized"):
            return reply({})
        if method == "tools/list":
            return reply({"tools": tool_definitions(client)})
        if method == "tools/call":
            params = message.get("params")
            if not isinstance(params, dict):
                return reply_error(-32602, "Invalid params: expected an object")
            arguments = params.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            try:
                result = call_tool(client, params.get("name", ""), arguments)
            except Exception as exc:  # tool-level failure -> isError result
                return reply(_text_result(redact(str(exc), _secret_values(client)), is_error=True))
            return reply(result)
        return reply_error(-32601, f"Method not found: {method}")
    except Exception as exc:  # server-level failure
        return reply_error(-32603, f"Internal error: {exc}")


def _secret_values(client: QobuzClient | None) -> list[str | None]:
    if client is None:
        return []
    return [client.app_id, client.app_secret, client.auth_token]


def serve(stdin: IO[str], stdout: IO[str], client: QobuzClient | None) -> None:
    """Read newline-delimited JSON-RPC messages, write responses."""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
        else:
            response = handle_request(client, message)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def main() -> None:
    # Build a client only from local env; never touches the network here.
    client = load_client() if config_status().credentials_present else None
    serve(sys.stdin, sys.stdout, client)


if __name__ == "__main__":
    main()
