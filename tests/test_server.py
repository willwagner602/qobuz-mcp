"""MCP registry behavior: tool schemas, write-tool validation gate, and
confirmation requirement at the server boundary (AC.1, AC.3)."""

import json

import pytest

from qobuz_mcp import server
from qobuz_mcp.qobuz_api import QobuzClient, WriteNotConfirmed


def _client(validation_passed: bool = False) -> QobuzClient:
    client = QobuzClient(app_id="app-id-1", app_secret="secret-1")
    client.validation_passed = validation_passed
    return client


def test_mcp_tool_registration():
    for client in (None, _client(False)):
        tools = server.tool_definitions(client)
        names = {t["name"] for t in tools}
        assert {"server_status", "search_tracks", "list_playlists", "get_playlist"} <= names
        for tool in tools:
            assert tool["description"]
            assert tool["inputSchema"]["type"] == "object"


def test_unvalidated_write_tool_not_exposed():
    for client in (None, _client(False)):
        names = {t["name"] for t in server.tool_definitions(client)}
        assert names.isdisjoint(set(server.WRITE_TOOLS))
    exposed = {t["name"] for t in server.tool_definitions(_client(True))}
    assert set(server.WRITE_TOOLS) <= exposed


def test_write_requires_confirmation():
    client = _client(True)  # exposed at MCP level, but transport must not be hit
    result = server.handle_request(
        client,
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "create_playlist", "arguments": {"name": "X"}},
        },
    )
    assert result is not None and result["result"]["isError"] is True
    assert "confirm" in result["result"]["content"][0]["text"].lower()


def test_write_requires_enablement():
    client = _client(False)
    result = server.handle_request(
        client,
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "create_playlist",
                "arguments": {"name": "X", "confirm": True},
            },
        },
    )
    assert result is not None and result["result"]["isError"] is True
    assert "not enabled" in result["result"]["content"][0]["text"]


def test_write_requires_strict_boolean_confirmation():
    client = _client(True)
    for bad in ("true", 1):
        result = server.handle_request(
            client,
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {
                    "name": "create_playlist",
                    "arguments": {"name": "X", "confirm": bad},
                },
            },
        )
        assert result is not None and result["result"]["isError"] is True
        assert "confirm" in result["result"]["content"][0]["text"].lower()


def test_invalid_jsonrpc_shape_rejected():
    result = server.handle_request(None, {"jsonrpc": "2.0", "id": 13})  # no method
    assert result is not None and result["error"]["code"] == -32600
    result = server.handle_request(None, {"jsonrpc": "2.0", "id": 14, "method": "tools/call", "params": []})
    assert result is not None and result["error"]["code"] == -32602


def test_search_tracks_fails_closed_without_credentials():
    result = server.handle_request(
        None,
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "search_tracks", "arguments": {"query": "x"}},
        },
    )
    assert result is not None and result["result"]["isError"] is True
    assert "Credentials required" in result["result"]["content"][0]["text"]


def test_initialize_and_unknown_method():
    init = server.handle_request(
        None,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        },
    )
    assert init is not None and init["result"]["protocolVersion"] == "2024-11-05"
    unknown = server.handle_request(None, {"jsonrpc": "2.0", "id": 2, "method": "nope"})
    assert unknown is not None and unknown["error"]["code"] == -32601
