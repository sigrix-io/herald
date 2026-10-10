"""Herald's MCP server: the protocol, the four tools, and both ways a client reaches them.

The protocol and tool tests talk to :class:`herald.mcp.Server` directly. The transport tests run
the server as a client meets it: ``herald mcp`` as a process on standard input and output, and the
HTTP server on a port of this machine. ``test_mcp_clients.py`` holds both to the official SDK.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import pytest

import herald.mcp
from conftest import TOKEN
from herald import Announcement, __version__, get_channel, prepare
from herald.mcp import ENDPOINT, MAX_REQUEST, PROTOCOL_VERSIONS, HttpServer, Server, check_listening, serve_stdio

LATEST = PROTOCOL_VERSIONS[0]

#: The errors 2026-07-28 calls its own. A client of that revision that meets one of these stays
#: in that revision rather than falling back to the handshake, so the probe must meet none.
MODERN_ERRORS = (-32020, -32021, -32022)


def request(method: str, params: Mapping[str, Any] | None = None, request_id: object = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = dict(params)
    return message


def initialize(version: object = LATEST, request_id: object = 1) -> dict[str, Any]:
    params = {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
    return request("initialize", params, request_id)


INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def call(server: Server, tool: str, arguments: object = None, version: str = LATEST) -> dict[str, Any]:
    params = {"name": tool, "arguments": {} if arguments is None else arguments}
    return server.respond(request("tools/call", params), version)["result"]


def text_of(result: Mapping[str, Any]) -> str:
    return "\n".join(block["text"] for block in result["content"])


@pytest.fixture
def server(ledger) -> Server:
    return Server(ledger)


# The protocol.


@pytest.mark.parametrize("version", PROTOCOL_VERSIONS)
def test_the_handshake_answers_in_the_revision_the_client_asked_for(server, version) -> None:
    result = server.respond(initialize(version))["result"]
    assert result["protocolVersion"] == version
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert {key: result["serverInfo"][key] for key in ("name", "version")} == {"name": "herald", "version": __version__}
    assert ("title" in result["serverInfo"]) == (version >= "2025-06-18")
    assert "call post with that draft id only after they agree" in result["instructions"]


@pytest.mark.parametrize("asked", ["2026-07-28", "1900-01-01", None, 20250618])
def test_a_client_asking_for_a_revision_it_does_not_speak_gets_its_newest(server, asked) -> None:
    assert server.respond(initialize(asked))["result"]["protocolVersion"] == LATEST


def test_a_2026_clients_probe_is_an_unknown_method_so_it_falls_back(server) -> None:
    """On stdio, a client of 2026-07-28 asks server/discover first, and falls back on any error but its own."""
    probe = request("server/discover", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}, "p-1")
    assert server.respond(probe) == {
        "jsonrpc": "2.0",
        "id": "p-1",
        "error": {"code": -32601, "message": "Method not found: server/discover"},
    }


@pytest.mark.parametrize("version", PROTOCOL_VERSIONS)
def test_the_tools_are_listed_with_what_each_revision_defines(server, version) -> None:
    tools = server.respond(request("tools/list"), version)["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["channels", "preview", "post", "check"]
    fields = {"name", "description", "inputSchema"}
    if version >= "2025-03-26":
        fields.add("annotations")
    if version >= "2025-06-18":
        fields.add("title")
    assert [set(tool) for tool in tools] == [fields] * 4


def test_post_takes_a_draft_id_and_nothing_else(server) -> None:
    tools = {tool["name"]: tool for tool in server.respond(request("tools/list"))["result"]["tools"]}
    schema = tools["post"]["inputSchema"]
    assert (list(schema["properties"]), schema["required"], schema["additionalProperties"]) == (
        ["draft_id"],
        ["draft_id"],
        False,
    )
    assert tools["preview"]["inputSchema"]["required"] == ["channels", "text"]
    assert tools["post"]["annotations"]["readOnlyHint"] is False
    assert tools["preview"]["annotations"]["openWorldHint"] is False, "a preview reaches no platform"


def test_notifications_and_a_clients_answers_get_no_answer(server) -> None:
    assert server.respond(INITIALIZED) is None
    assert server.respond({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}) is None
    assert server.respond({"jsonrpc": "2.0", "id": 7, "result": {}}) is None


def test_ping_answers_with_nothing(server) -> None:
    assert server.respond(request("ping")) == {"jsonrpc": "2.0", "id": 1, "result": {}}


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("ping", -32600),
        ({"id": 1, "method": "ping"}, -32600),
        ({"jsonrpc": "2.0", "id": None, "method": "ping"}, -32600),
        ({"jsonrpc": "2.0", "id": True, "method": "ping"}, -32600),
        ({"jsonrpc": "2.0", "id": 1, "method": 5}, -32600),
        ({"jsonrpc": "2.0", "id": 1}, -32600),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1]}, -32602),
        ([], -32600),
    ],
)
def test_a_malformed_message_is_refused(server, message, code) -> None:
    assert server.respond(message)["error"]["code"] == code


def test_a_batch_gets_an_answer_for_each_request_in_it(server) -> None:
    answers = server.respond([request("ping", request_id=1), INITIALIZED, request("resources/list", request_id=2)])
    assert [(answer["id"], "error" in answer) for answer in answers] == [(1, False), (2, True)]
    assert server.respond([INITIALIZED]) is None


def test_an_unknown_tool_is_a_protocol_error(server) -> None:
    answer = server.respond(request("tools/call", {"name": "delete_everything", "arguments": {}}))
    assert answer["error"] == {"code": -32602, "message": "Unknown tool: delete_everything"}


def test_a_failure_inside_a_tool_is_an_internal_error_without_a_credential(server, fake, monkeypatch, capsys) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError(f"broke near {TOKEN}")

    monkeypatch.setattr(herald.mcp, "preview", broken)
    answer = server.respond(
        request("tools/call", {"name": "preview", "arguments": {"channels": ["fake"], "text": "Hi"}})
    )
    assert answer["error"]["code"] == -32603
    assert answer["error"]["message"] == "tools/call failed: RuntimeError: broke near $FAKE_TOKEN"
    assert TOKEN not in capsys.readouterr().err


# The tools.


def test_a_post_with_no_draft_id_is_refused_and_posts_nothing(server, fake, ledger_dir) -> None:
    for arguments in (
        {},
        {"draft_id": ""},
        {"draft_id": "   "},
        {"draft_id": 5},
        {"text": "Hi"},
        {"draft_id": "x", "text": "Hi"},
    ):
        result = call(server, "post", arguments)
        assert result["isError"] is True, arguments
        assert "post takes one argument, draft_id: the id preview returned" in text_of(result)
        assert text_of(result).endswith(("Nothing was posted.", "It was given text."))
    assert fake.sent == []
    assert not (ledger_dir / "ledger.jsonl").exists()


def test_a_post_of_a_draft_no_preview_made_is_refused(server, fake) -> None:
    result = call(server, "post", {"draft_id": "0123456789ab"})
    assert result["isError"] is True
    assert text_of(result) == "no draft '0123456789ab': preview the announcement, then post the id it gives"
    assert fake.sent == []


def test_a_post_sends_exactly_the_previewed_text_and_only_once(server, fake) -> None:
    previewed = call(
        server, "preview", {"channels": ["fake"], "text": "Out now.", "link": "https://ex.co/2", "id": "v2"}
    )
    assert previewed["isError"] is False
    draft = previewed["structuredContent"]
    assert draft["items"] == [{"channel": "fake", "text": "Out now.\n\nhttps://ex.co/2", "count": 25, "limit": 40}]
    assert "\nOut now.\n\nhttps://ex.co/2\n" in text_of(previewed), "the model reads the text it is to show"
    assert f"call post with draft_id {draft['draft_id']}" in draft["next"]
    assert fake.sent == [], "a preview sends nothing"

    posted = call(server, "post", {"draft_id": draft["draft_id"]})
    assert posted["isError"] is False
    assert posted["structuredContent"]["outcomes"] == [
        {"channel": "fake", "status": "posted", "url": "https://fake.example/posts/1", "detail": "", "ok": True}
    ]
    assert text_of(posted) == "fake: posted: https://fake.example/posts/1"
    assert fake.sent == ["Out now.\n\nhttps://ex.co/2"]

    again = call(server, "post", {"draft_id": draft["draft_id"]})
    assert again["structuredContent"]["outcomes"][0]["status"] == "already-posted"
    assert fake.sent == ["Out now.\n\nhttps://ex.co/2"]


def test_a_preview_over_a_channels_limit_is_refused_with_its_count_and_keeps_nothing(server, fake, ledger_dir) -> None:
    result = call(server, "preview", {"channels": ["fake"], "text": "x" * 41})
    assert result["isError"] is True
    assert text_of(result) == "too long, so nothing was prepared: fake is 41 of 40"
    assert not (ledger_dir / "drafts.jsonl").exists()


@pytest.mark.parametrize(
    ("arguments", "said"),
    [
        ({"channels": ["nowhere"], "text": "Hi"}, "no channel called 'nowhere'; installed: "),
        (
            {"channels": "fake", "text": "Hi"},
            'channels must be a list of channel names, such as ["bluesky", "mastodon"]',
        ),
        ({"channels": [" "], "text": "Hi"}, "choose at least one channel"),
        ({"channels": ["fake"]}, "text is required"),
        ({"channels": ["fake"], "text": 5}, "text must be a string"),
        ({"channels": ["fake"], "text": "  "}, "an announcement needs some text"),
        ({"channels": ["fake"], "to": ["fake"], "text": "Hi"}, "unknown argument to: it takes channels, text,"),
        ({"channels": ["fake"], "text": "Hi", "channel_texts": {"stdout": "Hey"}}, "channel_texts names stdout"),
        ({"channels": ["fake"], "text": "Hi", "channel_texts": {"fake": 5}}, "channel_texts must map a channel's"),
    ],
)
def test_a_preview_says_what_is_wrong_with_its_arguments(server, fake, ledger_dir, arguments, said) -> None:
    result = call(server, "preview", arguments)
    assert result["isError"] is True
    assert said in text_of(result)
    assert not (ledger_dir / "drafts.jsonl").exists()


def test_channel_texts_give_a_channel_its_own_text(server, fake) -> None:
    arguments = {"channels": ["fake", "stdout"], "text": "For everyone", "channel_texts": {"FAKE": "For fake"}}
    items = call(server, "preview", arguments)["structuredContent"]["items"]
    assert {item["channel"]: item["text"] for item in items} == {"fake": "For fake", "stdout": "For everyone"}


def test_a_preview_says_which_channels_already_carry_the_announcement(server, fake) -> None:
    first = call(server, "preview", {"channels": ["fake"], "text": "Hi", "id": "v1"})
    call(server, "post", {"draft_id": first["structuredContent"]["draft_id"]})

    again = call(server, "preview", {"channels": ["fake", "stdout"], "text": "Hi there", "id": "v1"})
    assert [(entry["channel"], entry["state"]) for entry in again["structuredContent"]["held"]] == [("fake", "posted")]
    assert ": https://fake.example/posts/1. Posting this draft skips fake.)" in text_of(again)


def test_a_post_that_fails_anywhere_is_an_error_naming_each_outcome(server, fake, monkeypatch) -> None:
    draft = call(server, "preview", {"channels": ["fake", "stdout"], "text": "Hi", "id": "v1"})["structuredContent"]
    monkeypatch.setattr(fake, "behaviour", "refuse")

    result = call(server, "post", {"draft_id": draft["draft_id"]})
    assert result["isError"] is True
    outcomes = {
        outcome["channel"]: (outcome["status"], outcome["ok"]) for outcome in result["structuredContent"]["outcomes"]
    }
    assert outcomes == {"fake": ("failed", False), "stdout": ("posted", True)}
    assert "fake: not posted: 401 Unauthorized: token $FAKE_TOKEN is not valid" in text_of(result)
    assert TOKEN not in json.dumps(result)


def test_a_post_whose_result_went_unrecorded_waits_for_a_person(server, fake, monkeypatch) -> None:
    draft = call(server, "preview", {"channels": ["fake"], "text": "Hi", "id": "v1"})["structuredContent"]["draft_id"]
    monkeypatch.setattr(fake, "behaviour", "lose")

    for _ in range(2):
        result = call(server, "post", {"draft_id": draft})
        assert result["isError"] is True
        assert result["structuredContent"]["outcomes"][0]["status"] == "needs-a-person"
        assert "herald resolve v1 fake --posted" in text_of(result)
        assert TOKEN not in json.dumps(result)
    assert len(fake.sent) == 1, "never tried again on its own"


def test_the_channels_tool_names_what_is_missing_and_never_a_value(server, fake, monkeypatch) -> None:
    listed = call(server, "channels")
    entry = next(entry for entry in listed["structuredContent"]["channels"] if entry["name"] == "fake")
    assert entry == {
        "name": "fake",
        "label": "Fake",
        "limit": 40,
        "credentials": ["FAKE_TOKEN"],
        "missing": [],
        "source": "conftest",
    }
    assert "fake (Fake): limit 40; reads FAKE_TOKEN, all set. From conftest." in text_of(listed)
    assert "stdout (Standard output): no limit; needs no credentials." in text_of(listed)
    assert TOKEN not in json.dumps(listed)

    monkeypatch.delenv("FAKE_TOKEN")
    assert "fake (Fake): limit 40; reads FAKE_TOKEN; not set: FAKE_TOKEN." in text_of(call(server, "channels"))


def test_check_says_whether_each_channel_is_ready_without_showing_a_value(server, fake, monkeypatch) -> None:
    result = call(server, "check", {"channels": ["fake"]})
    assert (result["isError"], result["structuredContent"]) == (
        False,
        {
            "ready": True,
            "channels": [
                {"channel": "fake", "ok": True, "detail": "credentials are set (not checked with the platform)"}
            ],
        },
    )
    assert TOKEN not in json.dumps(result)

    monkeypatch.delenv("FAKE_TOKEN")
    result = call(server, "check", {"channels": ["fake"]})
    assert (result["isError"], result["structuredContent"]["ready"]) == (False, False)
    assert text_of(result) == "fake: not ready: set FAKE_TOKEN"

    unknown = call(server, "check", {"channels": ["fake", "nowhere"]})
    assert unknown["isError"] is True
    assert fake.checks == 2, "a name no channel has is refused before any channel is checked"


def test_check_with_no_channels_checks_every_one(server, fake, monkeypatch, no_network) -> None:
    for name in [name for name in os.environ if name.startswith("HERALD_")]:
        monkeypatch.delenv(name)
    result = call(server, "check")
    checked = [entry["channel"] for entry in result["structuredContent"]["channels"]]
    assert {"bluesky", "fake", "stdout"} <= set(checked)
    assert checked == sorted(checked)


def test_arguments_that_are_not_an_object_are_refused(server) -> None:
    result = call(server, "channels", [1])
    assert (result["isError"], text_of(result)) == (True, "channels's arguments must be an object")


@pytest.mark.parametrize("version", PROTOCOL_VERSIONS)
def test_structured_content_comes_from_2025_06_18(server, fake, version) -> None:
    result = call(server, "channels", version=version)
    assert ("structuredContent" in result) == (version >= "2025-06-18")
    assert "fake (Fake)" in text_of(result)


# Standard input and output.


def test_stdio_answers_each_line_and_says_when_one_is_not_json(server) -> None:
    lines = [json.dumps(request("ping")).encode(), b"not json", b"", json.dumps(INITIALIZED).encode()]
    output = io.BytesIO()
    serve_stdio(server, io.BytesIO(b"\n".join(lines) + b"\n"), output)
    assert [json.loads(line) for line in output.getvalue().splitlines()] == [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error: a line that is not JSON"}},
    ]


def test_stdio_keeps_to_the_revision_the_handshake_agreed(server, fake) -> None:
    call_channels = request("tools/call", {"name": "channels", "arguments": {}}, 2)
    lines = b"".join(json.dumps(message).encode() + b"\n" for message in (initialize("2025-03-26"), call_channels))
    output = io.BytesIO()
    serve_stdio(server, io.BytesIO(lines), output)
    answers = [json.loads(line) for line in output.getvalue().splitlines()]
    assert answers[0]["result"]["protocolVersion"] == "2025-03-26"
    assert "structuredContent" not in answers[1]["result"]


def test_stdio_writes_each_answer_on_one_line_of_ascii(server, fake) -> None:
    arguments = {"channels": ["fake"], "text": "Line one\N{LINE SEPARATOR}line two \N{PARTY POPPER}", "id": "v1"}
    output = io.BytesIO()
    serve_stdio(
        server,
        io.BytesIO(json.dumps(request("tools/call", {"name": "preview", "arguments": arguments})).encode()),
        output,
    )
    written = output.getvalue()
    assert written.isascii() and written.count(b"\n") == 1 and written.endswith(b"\n")
    assert json.loads(written)["result"]["structuredContent"]["items"][0]["text"] == arguments["text"]


def test_herald_mcp_serves_a_client_and_keeps_what_channels_print_off_the_protocol(tmp_path) -> None:
    """The whole session a client holds, against the command itself, with the stdout channel posting."""
    draft = prepare(Announcement(text="Hello over stdio", id="stdio-1"), [get_channel("stdout")]).id
    messages = [
        request("server/discover", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}, 0),
        initialize("2025-06-18"),
        INITIALIZED,
        request("tools/call", {"name": "post", "arguments": {}}, 2),
        request(
            "tools/call",
            {"name": "preview", "arguments": {"channels": ["stdout"], "text": "Hello over stdio", "id": "stdio-1"}},
            3,
        ),
        request("tools/call", {"name": "post", "arguments": {"draft_id": draft}}, 4),
        request("tools/call", {"name": "post", "arguments": {"draft_id": draft}}, 5),
    ]
    finished = subprocess.run(  # noqa: S603 - herald itself, with the test's own arguments
        [sys.executable, "-m", "herald", "mcp", "--ledger-dir", str(tmp_path / "ledger")],
        input=b"".join(json.dumps(message).encode() + b"\n" for message in messages),
        capture_output=True,
        timeout=60,
        check=True,
    )
    answers = {answer["id"]: answer for answer in map(json.loads, finished.stdout.splitlines())}
    assert sorted(answers) == [0, 1, 2, 3, 4, 5], "standard output carries answers and nothing else"
    assert answers[0]["error"]["code"] == -32601
    assert answers[1]["result"]["protocolVersion"] == "2025-06-18"
    assert answers[2]["result"]["isError"] is True
    assert answers[3]["result"]["structuredContent"]["draft_id"] == draft
    assert answers[4]["result"]["content"][0]["text"] == "stdout: posted"
    assert answers[5]["result"]["structuredContent"]["outcomes"][0]["status"] == "already-posted"
    printed = finished.stderr.decode().replace("\r\n", "\n")
    assert printed.count("--- stdout ---\nHello over stdio\n--- end ---\n") == 1


# HTTP.


@pytest.fixture
def serve(ledger) -> Iterator[Callable[..., int]]:
    """Start the HTTP server on a free port of this machine, with a token or without; give its port."""
    started: list[tuple[HttpServer, threading.Thread]] = []

    def start(token: str | None = None) -> int:
        http_server = HttpServer(Server(ledger), "127.0.0.1", 0, token)
        thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        started.append((http_server, thread))
        return http_server.server_address[1]

    yield start
    for http_server, thread in started:
        http_server.shutdown()
        http_server.server_close()
        thread.join()


JSON_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def exchange(
    port: int,
    message: object = None,
    *,
    method: str = "POST",
    path: str = ENDPOINT,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    connection: http.client.HTTPConnection | None = None,
) -> tuple[int, http.client.HTTPMessage, Any]:
    """Send one request as an MCP client would, without a proxy. Gives the status, headers and JSON body."""
    own = connection is None
    connection = connection or http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    if body is None and message is not None:
        body = json.dumps(message).encode()
    try:
        connection.request(method, path, body=body, headers={**JSON_HEADERS, **(headers or {})})
        answer = connection.getresponse()
        data = answer.read()
    finally:
        if own:
            connection.close()
    return answer.status, answer.headers, json.loads(data) if data else None


def speaking(version: str = LATEST) -> dict[str, str]:
    return {"MCP-Protocol-Version": version}


def test_http_serves_the_handshake_the_tools_and_a_post_on_one_connection(serve, fake) -> None:
    port = serve()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    status, headers, answer = exchange(port, initialize(), connection=connection)
    assert (status, headers["Content-Type"], answer["result"]["protocolVersion"]) == (200, "application/json", LATEST)
    assert headers["Mcp-Session-Id"] is None, "the server keeps no session"
    assert exchange(port, INITIALIZED, headers=speaking(), connection=connection)[::2] == (202, None)

    arguments = {"channels": ["fake"], "text": "Over HTTP", "id": "h1"}
    preview = request("tools/call", {"name": "preview", "arguments": arguments}, 2)
    status, _, answer = exchange(port, preview, headers=speaking(), connection=connection)
    draft = answer["result"]["structuredContent"]["draft_id"]
    post = request("tools/call", {"name": "post", "arguments": {"draft_id": draft}}, 3)
    status, _, answer = exchange(port, post, headers=speaking(), connection=connection)
    assert (status, answer["id"], answer["result"]["isError"]) == (200, 3, False)
    assert fake.sent == ["Over HTTP"]
    connection.close()


def test_http_refuses_a_post_with_no_draft_id(serve, fake) -> None:
    port = serve()
    status, _, answer = exchange(port, request("tools/call", {"name": "post", "arguments": {}}), headers=speaking())
    assert (status, answer["result"]["isError"]) == (200, True)
    assert "draft_id: the id preview returned" in text_of(answer["result"])
    assert fake.sent == []


def test_http_takes_a_request_that_names_no_revision_as_2025_03_26(serve, fake) -> None:
    port = serve()
    _, _, answer = exchange(port, request("tools/call", {"name": "channels", "arguments": {}}))
    assert "structuredContent" not in answer["result"]
    _, _, answer = exchange(port, request("tools/call", {"name": "channels", "arguments": {}}), headers=speaking())
    assert "structuredContent" in answer["result"]


def test_http_refuses_a_revision_it_does_not_speak_so_a_2026_client_falls_back(serve) -> None:
    port = serve()
    probe = request("server/discover", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}, "p-1")
    status, _, answer = exchange(port, probe, headers={**speaking("2026-07-28"), "Mcp-Method": "server/discover"})
    assert (status, answer["id"]) == (400, "p-1")
    assert answer["error"]["code"] not in MODERN_ERRORS
    assert "It speaks 2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05" in answer["error"]["message"]

    status, _, answer = exchange(port, initialize(), headers=speaking("2026-07-28"))
    assert (status, answer["result"]["protocolVersion"]) == (200, LATEST), (
        "the handshake names its revision in its body"
    )


@pytest.mark.parametrize("origin", ["https://example.com", "http://localhost:6274", "null"])
def test_http_refuses_a_browser_page(serve, fake, origin) -> None:
    """A page that rebinds a name of its own to this machine still sends its Origin, and is refused."""
    port = serve()
    arguments = {"channels": ["fake"], "text": "Hi", "id": "v1"}
    status, _, answer = exchange(
        port, request("tools/call", {"name": "preview", "arguments": arguments}), headers={"Origin": origin}
    )
    assert (status, answer["error"]["message"]) == (403, "a request from a browser page is refused")


def test_http_with_a_token_refuses_a_request_without_it(serve) -> None:
    secret = "s3cret-" * 6
    port = serve(secret)
    for authorization in (None, "Bearer wrong", f"Basic {secret}", f"Bearer {secret}x", secret):
        headers = {} if authorization is None else {"Authorization": authorization}
        status, answer_headers, answer = exchange(port, request("ping"), headers=headers)
        assert (status, answer_headers["WWW-Authenticate"]) == (401, "Bearer"), authorization
        assert secret not in json.dumps(answer)
    for authorization in (f"Bearer {secret}", f"bearer {secret}"):
        status, _, answer = exchange(port, request("ping"), headers={"Authorization": authorization})
        assert (status, answer["result"]) == (200, {})


def test_http_refuses_what_is_not_a_message_to_its_endpoint(serve) -> None:
    port = serve()
    assert exchange(port, request("ping"), path="/")[0] == 404
    assert exchange(port, request("ping"), path=f"{ENDPOINT}/other")[0] == 404
    status, headers, _ = exchange(port, method="GET")
    assert (status, headers["Allow"]) == (405, "POST")
    assert exchange(port, method="DELETE")[0] == 405
    assert exchange(port, body=b"{}", headers={"Content-Type": "text/plain"})[0] == 415
    assert exchange(port, request("ping"), headers={"Accept": "text/html"})[0] == 406
    assert exchange(port, request("ping"), headers={"Accept": "*/*"})[0] == 200
    status, _, answer = exchange(port, body=b"not json")
    assert (status, answer["error"]["code"]) == (400, -32700)
    assert exchange(port, {"jsonrpc": "2.0"})[0] == 400
    assert exchange(port, body=b"", headers={"Content-Length": str(MAX_REQUEST + 1)})[0] == 413


def test_http_reads_a_refused_requests_body_so_no_reset_overtakes_the_refusal(serve) -> None:
    """A server that closes on bytes it never read resets the connection, and on some systems a
    reset discards the refusal before the client reads it. So a refused body is read, and the
    connection ends cleanly: after it, the client can still send, which a reset would forbid.
    """
    port = serve("t" * 40)
    body = b" " * 65536
    head = (
        f"POST {ENDPOINT} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
        f"Authorization: Bearer wrong\r\nContent-Length: {len(body)}\r\n\r\n"
    )
    received = b""
    with socket.create_connection(("127.0.0.1", port), timeout=30) as connection:
        connection.sendall(head.encode() + body)
        while chunk := connection.recv(65536):
            received += chunk
        time.sleep(0.2)  # time for a reset, had there been one, to arrive
        connection.send(b"x")
    assert received.startswith(b"HTTP/1.1 401 ")


def test_http_needs_a_requests_length(serve) -> None:
    port = serve()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    connection.putrequest("POST", ENDPOINT)
    connection.putheader("Content-Type", "application/json")
    connection.endheaders()
    assert connection.getresponse().status == 411
    connection.close()


def test_http_will_not_listen_beyond_this_machine_without_a_token(ledger) -> None:
    with pytest.raises(
        ValueError, match="lets other machines reach the server, so it needs a token: set HERALD_MCP_TOKEN"
    ):
        HttpServer(Server(ledger), "0.0.0.0", 0, None)  # noqa: S104 - refused, which is the point


@pytest.mark.parametrize("token", ["short", "x" * 31, "\N{LATIN SMALL LETTER E WITH ACUTE}" * 40, "has a space " * 4])
def test_a_token_that_could_be_guessed_or_garbled_is_refused(token) -> None:
    with pytest.raises(ValueError, match="HERALD_MCP_TOKEN"):
        check_listening("127.0.0.1", token)


def test_this_machine_needs_no_token_and_any_address_takes_one() -> None:
    check_listening("127.0.0.1", None)
    check_listening("localhost", None)
    check_listening("0.0.0.0", "t" * 32)  # noqa: S104 - with a token, which is the point


# The command.


def run_herald(*arguments: str, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``herald`` as a process, so a server that started by mistake ends in a timeout, not a hang."""
    return subprocess.run(  # noqa: S603 - herald itself, with the test's own arguments
        [sys.executable, "-m", "herald", *arguments],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def test_mcp_host_and_port_go_with_http() -> None:
    finished = run_herald("mcp", "--port", "9000")
    assert (finished.returncode, finished.stderr) == (1, "herald: --host and --port go with --http\n")


def test_mcp_http_will_not_listen_on_every_address_without_a_token() -> None:
    env = {name: value for name, value in os.environ.items() if name != "HERALD_MCP_TOKEN"}
    finished = run_herald("mcp", "--http", "--host", "0.0.0.0", "--port", "0", env=env)  # noqa: S104 - refused
    assert finished.returncode == 1
    assert "so it needs a token: set HERALD_MCP_TOKEN" in finished.stderr
