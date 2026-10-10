"""Herald's MCP server held to the official MCP SDK's client, over both of its transports.

``mcp`` is that SDK. It is a development dependency only, and Herald itself never imports it,
which ``test_rules.py`` checks. Its client opens as a client of protocol revision 2026-07-28 does:
it asks in that revision first and falls back to the handshake when the server does not speak it.
So these tests hold the server to that fallback too, which is how Claude Code opens.
"""

from __future__ import annotations

import sys
import threading
from typing import Any

import pytest

from herald.mcp import HttpServer, Server

mcp = pytest.importorskip("mcp")
anyio = pytest.importorskip("anyio")
httpx2 = pytest.importorskip("httpx2")
stdio = pytest.importorskip("mcp.client.stdio")
streamable_http = pytest.importorskip("mcp.client.streamable_http")


async def _session(client: Any) -> dict[str, Any]:
    """What an assistant does: list the tools, preview, post the draft, and post it again."""
    seen: dict[str, Any] = {"version": client.session.protocol_version}
    seen["tools"] = [tool.name for tool in (await client.list_tools()).tools]
    refused = await client.call_tool("post", {})
    seen["refused"] = (refused.is_error, refused.content[0].text)
    previewed = await client.call_tool("preview", {"channels": ["stdout"], "text": "Hello from the SDK", "id": "sdk"})
    seen["items"] = previewed.structured_content["items"]
    draft = previewed.structured_content["draft_id"]
    seen["posted"] = [(await client.call_tool("post", {"draft_id": draft})).content[0].text for _ in range(2)]
    return seen


def _assert_a_whole_session(seen: dict[str, Any]) -> None:
    assert seen["version"] == "2025-11-25", "the client fell back from 2026-07-28 to the handshake"
    assert seen["tools"] == ["channels", "preview", "post", "check"]
    assert seen["refused"][0] is True
    assert "post takes one argument, draft_id" in seen["refused"][1]
    assert seen["items"] == [{"channel": "stdout", "text": "Hello from the SDK", "count": 18, "limit": None}]
    assert seen["posted"][0] == "stdout: posted"
    assert seen["posted"][1].startswith("stdout: already posted, so not again")


def test_the_sdk_client_lists_and_calls_the_tools_on_stdio(tmp_path) -> None:
    command = [sys.executable, "-m", "herald", "mcp", "--ledger-dir", str(tmp_path / "ledger")]
    params = stdio.StdioServerParameters(command=command[0], args=command[1:])

    async def run() -> dict[str, Any]:
        async with mcp.Client(params) as client:
            return await _session(client)

    _assert_a_whole_session(anyio.run(run))


def test_the_sdk_client_lists_and_calls_the_tools_over_http_with_a_token(ledger) -> None:
    token = "sdk-token-" * 4
    http_server = HttpServer(Server(ledger), "127.0.0.1", 0, token)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()

    async def run() -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx2.AsyncClient(headers=headers, trust_env=False) as http_client:
            transport = streamable_http.streamable_http_client(http_server.url, http_client=http_client)
            async with mcp.Client(transport) as client:
                return await _session(client)

    try:
        _assert_a_whole_session(anyio.run(run))
    finally:
        http_server.shutdown()
        http_server.server_close()
        thread.join()
