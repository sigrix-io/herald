"""Herald as an MCP server, so an assistant or a workflow tool can announce.

``herald mcp`` speaks the Model Context Protocol over standard input and output, for an assistant
that runs Herald on its own machine. ``herald mcp --http`` speaks it over HTTP, for a tool that
reaches a server by its address. Both use the standard library only.

The tools are ``channels``, ``preview``, ``post`` and ``check``. ``post`` takes a draft id that
``preview`` returned and nothing else, so a model posts exactly the text a preview showed, and the
tools' descriptions tell it to show that text to the person and to post only once they agree.
Each result comes as text, which is what a model reads, and, from protocol revision 2025-06-18
on, as ``structuredContent`` too, which is what a workflow reads.

The server speaks the revisions that open with an ``initialize`` handshake, 2024-11-05 to
2025-11-25. A client that also speaks 2026-07-28, which has no handshake, asks in that revision
first, is refused in a way that revision does not recognise as its own, and falls back to the
handshake, as 2026-07-28 says it should.

Over HTTP the server keeps no session: it reads each request's revision from its
``MCP-Protocol-Version`` header. It listens on 127.0.0.1 unless told otherwise. It refuses any
request a browser page makes, which is what keeps a web page from reaching it through DNS
rebinding. When ``HERALD_MCP_TOKEN`` is set, every request must carry that token, and without one
the server will not listen beyond this machine.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import socket
import sys
import threading
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, BinaryIO

from herald import __version__
from herald.announcement import Announcement
from herald.channels import ChannelConflict, UnknownChannel, discover_channels
from herald.drafts import TooLong
from herald.ledger import HOLDING, Ledger, LedgerBusy
from herald.posting import UnknownDraft, check, post, preview, redact

#: The protocol revisions this server speaks, newest first. A client that asks for another gets the newest.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

#: The revision an HTTP request that names none is taken to speak, as the protocol says.
UNNAMED_VERSION = "2025-03-26"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
#: The one path the HTTP server answers on.
ENDPOINT = "/mcp"
#: The largest request the HTTP server reads. An announcement takes a few kilobytes.
MAX_REQUEST = 1 << 20
#: The shortest token the HTTP server takes. A shorter one could be guessed.
SHORTEST_TOKEN = 32
#: Where the HTTP server reads its token from. Never a command-line option, which other users can see.
TOKEN_VARIABLE = "HERALD_MCP_TOKEN"  # noqa: S105 - the variable's name, not a token

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    "Herald posts one announcement to the channels a person chooses, such as Bluesky, Mastodon, Discord, "
    "Slack, Telegram, a LinkedIn profile or page, or X. Call preview first: it fits the announcement to each "
    "channel and returns a draft id and the exact text each channel will get, and it sends nothing. Show the "
    "person every channel's text exactly as preview returned it, and call post with that draft id only after "
    "they agree. post sends exactly that text, once per announcement and channel. To change anything, preview "
    "again and show the new draft."
)

_NO_ARGUMENTS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

_NAMES = {"type": "array", "items": {"type": "string"}}

_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "channels",
        "title": "List channels",
        "description": (
            "List the channels Herald can post to: each one's name, the longest post it takes, the "
            "credentials it reads and which of them are not set. Sends nothing."
        ),
        "inputSchema": _NO_ARGUMENTS,
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "preview",
        "title": "Preview an announcement",
        "description": (
            "Fit an announcement to each channel and return a draft: its id, and the exact text each "
            "channel will get, counted the way that platform counts. Sends nothing and makes no network "
            "call. Text over a channel's limit is refused with its count, never cut: shorten it, or give "
            "that channel its own text in channel_texts, and preview again. Before posting, show the "
            "person each channel's text exactly as it is returned here, and ask them to agree to it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "channels": {
                    **_NAMES,
                    "minItems": 1,
                    "description": "The channels to post to, by name, as the channels tool lists them.",
                },
                "text": {"type": "string", "description": "The announcement's text."},
                "link": {
                    "type": "string",
                    "description": "A link, added to each channel's text where the text does not already hold it.",
                },
                "title": {"type": "string", "description": "The announcement's title."},
                "id": {
                    "type": "string",
                    "description": (
                        "What makes the announcement post once, such as a release tag or a post's slug. Give "
                        "one whenever the thing announced has a name: drafts with the same id never post "
                        "twice. Without it, the id comes from the link and the title, or from the text when "
                        "there is neither, so a reworded announcement with neither counts as a new one."
                    ),
                },
                "channel_texts": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Text for a channel to use instead of text, by the channel's name.",
                },
            },
            "required": ["channels", "text"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "post",
        "title": "Post a previewed draft",
        "description": (
            "Post a draft that preview returned, to each of its channels. It takes nothing but the draft "
            "id, so what goes out is exactly the text preview returned. Call it only after the person has "
            "seen that text and agreed to it. A channel that already carries the announcement is skipped, "
            "so it never posts the same announcement twice. A post whose result went unrecorded is "
            "reported for a person to check, and never tried again on its own."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"draft_id": {"type": "string", "description": "The draft id preview returned."}},
            "required": ["draft_id"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
    {
        "name": "check",
        "title": "Check credentials",
        "description": (
            "Check each channel's credentials: that they are set and, where the platform can say, that they "
            "work. Names any variable that is missing and never shows a value. Posts nothing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "channels": {
                    **_NAMES,
                    "description": "The channels to check, by name. Leave it out to check every channel.",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
)

_NO_DRAFT = (
    "post takes one argument, draft_id: the id preview returned, so that what goes out is exactly the text "
    "the person was shown. Preview the announcement, show the person each channel's text, and post the draft "
    "they agree to. Nothing was posted."
)


class _BadParams(Exception):
    """A request's params do not fit its method: a protocol error, answered with ``INVALID_PARAMS``."""


class _Refused(Exception):
    """A tool cannot do what it was asked. The model is told why, so it can ask again differently."""


def negotiate(requested: object) -> str:
    """The revision to speak with a client that asked for ``requested``: that one if this server speaks it."""
    return requested if isinstance(requested, str) and requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]


def _since(version: str, revision: str) -> bool:
    """Whether ``version`` is ``revision`` or newer."""
    return PROTOCOL_VERSIONS.index(version) <= PROTOCOL_VERSIONS.index(revision)


def _failure(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _is_request(message: object) -> bool:
    return isinstance(message, dict) and "method" in message and "id" in message


def _requested_version(message: object) -> object:
    """The revision an ``initialize`` request asks for, or ``None`` when ``message`` is no such request."""
    if not (_is_request(message) and message["method"] == "initialize"):  # type: ignore[index]
        return None
    params = message.get("params")  # type: ignore[union-attr]
    return params.get("protocolVersion") if isinstance(params, dict) else None


def _is_initialize(message: object) -> bool:
    return _is_request(message) and message["method"] == "initialize"  # type: ignore[index]


def _encode(message: object) -> bytes:
    """One JSON-RPC message as bytes. Escaped to ASCII, so no character in it can read as a line's end."""
    return json.dumps(message, separators=(",", ":")).encode("ascii")


def _described(error: BaseException) -> str:
    """An exception in a line, with any channel's credential in it replaced by the credential's name."""
    text = f"{type(error).__name__}: {error}"
    try:
        names = [name for found in discover_channels() for name in found.channel.credentials]
    except Exception:
        names = []
    return redact(text, names, os.environ)


class Server:
    """Herald's tools, answering MCP messages. It keeps no session, so one serves every client.

    Tool calls run one at a time, which keeps two clients from interleaving their posts.
    """

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self._calls = threading.Lock()
        self._methods: dict[str, Callable[[dict[str, Any], str], dict[str, Any]]] = {
            "initialize": self._initialize,
            "ping": lambda params, version: {},
            "tools/list": self._list_tools,
            "tools/call": self._call_tool,
        }
        self._tools: dict[str, Callable[[dict[str, Any]], tuple[str, dict[str, Any], bool]]] = {
            "channels": self._channels,
            "preview": self._preview,
            "post": self._post,
            "check": self._check,
        }

    def respond(self, message: Any, version: str = PROTOCOL_VERSIONS[0]) -> Any:
        """The answer to one JSON-RPC message or a batch of them, shaped for ``version``.

        ``None`` when nothing is due: a notification, a client's response, or a batch of those.
        """
        if isinstance(message, list):
            if not message:
                return _failure(None, INVALID_REQUEST, "Invalid request: an empty batch")
            answers = [answer for item in message if (answer := self._answer(item, version)) is not None]
            return answers or None
        return self._answer(message, version)

    def _answer(self, message: Any, version: str) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _failure(None, INVALID_REQUEST, "Invalid request: not a JSON-RPC 2.0 message")
        if "method" not in message:
            if "result" in message or "error" in message:
                return None  # a client's answer; this server asks nothing, so there is nothing to match it to
            return _failure(None, INVALID_REQUEST, "Invalid request: neither a request nor a notification")
        if "id" not in message:
            return None  # a notification: initialized and cancelled need nothing from this server
        request_id, method = message["id"], message["method"]
        if isinstance(request_id, bool) or not isinstance(request_id, str | int | float):
            return _failure(None, INVALID_REQUEST, "Invalid request: a request's id must be a string or a number")
        if not isinstance(method, str):
            return _failure(request_id, INVALID_REQUEST, "Invalid request: method must be a string")
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _failure(request_id, INVALID_PARAMS, "Invalid params: params must be an object")
        handler = self._methods.get(method)
        if handler is None:
            return _failure(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        try:
            result = handler(params, version)
        except _BadParams as problem:
            return _failure(request_id, INVALID_PARAMS, str(problem))
        except Exception as error:
            described = _described(error)
            print(f"herald mcp: {method} failed: {described}", file=sys.stderr)
            return _failure(request_id, INTERNAL_ERROR, f"{method} failed: {described}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _initialize(self, params: dict[str, Any], version: str) -> dict[str, Any]:
        negotiated = negotiate(params.get("protocolVersion"))
        info = {"name": "herald", "version": __version__}
        if _since(negotiated, "2025-06-18"):
            info["title"] = "Herald"
        return {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": info,
            "instructions": INSTRUCTIONS,
        }

    def _list_tools(self, params: dict[str, Any], version: str) -> dict[str, Any]:
        listed = []
        for tool in _TOOLS:
            entry = {key: tool[key] for key in ("name", "description", "inputSchema")}
            if _since(version, "2025-03-26"):
                entry["annotations"] = {"title": tool["title"], **tool["annotations"]}
            if _since(version, "2025-06-18"):
                entry["title"] = tool["title"]
            listed.append(entry)
        return {"tools": listed}

    def _call_tool(self, params: dict[str, Any], version: str) -> dict[str, Any]:
        name = params.get("name")
        run = self._tools.get(name) if isinstance(name, str) else None
        if run is None:
            raise _BadParams(f"Unknown tool: {name}")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        with self._calls:
            try:
                if not isinstance(arguments, dict):
                    raise _Refused(f"{name}'s arguments must be an object")
                text, data, failed = run(arguments)
            except _Refused as refusal:
                return {"content": [{"type": "text", "text": str(refusal)}], "isError": True}
            except (
                TooLong,
                UnknownDraft,
                UnknownChannel,
                ChannelConflict,
                LedgerBusy,
                LookupError,
                ValueError,
            ) as error:
                return {"content": [{"type": "text", "text": str(error)}], "isError": True}
            except OSError as error:
                where = f": {error.filename}" if error.filename else ""
                return {"content": [{"type": "text", "text": f"{error.strerror or error}{where}"}], "isError": True}
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": failed}
        if _since(version, "2025-06-18"):
            result["structuredContent"] = data
        return result

    # The tools. Each returns its text for a model, its data for a program, and whether it failed.

    def _channels(self, arguments: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
        _only(arguments)
        listed = []
        for found in discover_channels():
            channel = found.channel()
            listed.append(
                {
                    "name": found.name,
                    "label": found.channel.label,
                    "limit": channel.limit,
                    "credentials": list(found.channel.credentials),
                    "missing": channel.missing_credentials(os.environ),
                    "source": found.source,
                }
            )
        lines = []
        for entry in listed:
            limit = "no limit" if entry["limit"] is None else f"limit {entry['limit']}"
            if not entry["credentials"]:
                needs = "needs no credentials"
            elif entry["missing"]:
                needs = f"reads {', '.join(entry['credentials'])}; not set: {', '.join(entry['missing'])}"
            else:
                needs = f"reads {', '.join(entry['credentials'])}, all set"
            lines.append(f"{entry['name']} ({entry['label']}): {limit}; {needs}. From {entry['source']}.")
        return "\n".join(lines) or "No channels are installed.", {"channels": listed}, False

    def _preview(self, arguments: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
        _only(arguments, "channels", "text", "link", "title", "id", "channel_texts")
        names = _names(arguments, required=True)
        channel_texts = arguments.get("channel_texts") or {}
        if not isinstance(channel_texts, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in channel_texts.items()
        ):
            raise _Refused("channel_texts must map a channel's name to its text")
        unused = sorted(key for key in channel_texts if key.strip().lower() not in names)
        if unused:
            raise _Refused(
                f"channel_texts names {', '.join(unused)}, which channels does not: add it there or leave it out"
            )
        announcement = Announcement(
            text=_string(arguments, "text", required=True),
            link=_string(arguments, "link").strip(),
            title=_string(arguments, "title").strip(),
            id=_string(arguments, "id").strip(),
            per_channel=channel_texts,
        )
        draft = preview(announcement, names, self.ledger)
        held = {}
        for item in draft.items:
            entry = self.ledger.state(draft.announcement_id, item.channel)
            if entry is not None and entry.state in HOLDING:
                held[item.channel] = entry
        next_step = (
            f"Show the person each channel's text exactly as it is here. Once they agree to it, call post with "
            f"draft_id {draft.id}. To change anything, preview again: a draft never changes."
        )
        lines = [f"Draft {draft.id}, for announcement {draft.announcement_id}. Nothing was sent."]
        for item in draft.items:
            size = f"{item.count} of {item.limit}" if item.limit is not None else f"{item.count}, no limit"
            lines.append(f"\n--- {item.channel}: {size} ---\n{item.text}\n--- end of {item.channel} ---")
            entry = held.get(item.channel)
            if entry is not None and entry.state == "posted":
                where = f": {entry.url}" if entry.url else ""
                lines.append(f"(Already posted {entry.at}{where}. Posting this draft skips {item.channel}.)")
            elif entry is not None:
                lines.append(
                    f"(A post to {item.channel} began {entry.at} and its result was never recorded. Posting this "
                    f"draft skips {item.channel} until a person checks it.)"
                )
        lines.append(f"\n{next_step}")
        data = {
            "draft_id": draft.id,
            "announcement_id": draft.announcement_id,
            "items": [asdict(item) for item in draft.items],
            "held": [asdict(entry) for entry in held.values()],
            "next": next_step,
        }
        return "\n".join(lines), data, False

    def _post(self, arguments: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
        draft_id = arguments.get("draft_id")
        others = sorted(set(arguments) - {"draft_id"})
        if others or not isinstance(draft_id, str) or not draft_id.strip():
            given = f" It was given {', '.join(others)}." if others else ""
            raise _Refused(_NO_DRAFT + given)
        outcomes = post(draft_id.strip(), self.ledger)
        data = {
            "draft_id": draft_id.strip(),
            "outcomes": [{**asdict(outcome), "ok": outcome.ok} for outcome in outcomes],
        }
        return "\n".join(outcome.describe() for outcome in outcomes), data, not all(outcome.ok for outcome in outcomes)

    def _check(self, arguments: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
        _only(arguments, "channels")
        results = check(_names(arguments, required=False) or None)
        lines = [
            f"{name}: {'ready' if result.ok else 'not ready'}" + (f": {result.detail}" if result.detail else "")
            for name, result in results
        ]
        data = {
            "ready": all(result.ok for _, result in results),
            "channels": [{"channel": name, **asdict(result)} for name, result in results],
        }
        return "\n".join(lines) or "No channels are installed.", data, False


def _only(arguments: Mapping[str, Any], *allowed: str) -> None:
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        takes = f"it takes {', '.join(allowed)}" if allowed else "it takes none"
        raise _Refused(f"unknown argument {', '.join(unknown)}: {takes}")


def _string(arguments: Mapping[str, Any], name: str, *, required: bool = False) -> str:
    value = arguments.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise _Refused(f"{name} must be a string" if value is not None else f"{name} is required")
    return value


def _names(arguments: Mapping[str, Any], *, required: bool) -> list[str]:
    """The channel names in ``arguments["channels"]``, in lower case and in order, once each."""
    value = arguments.get("channels")
    if value is None and not required:
        return []
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise _Refused('channels must be a list of channel names, such as ["bluesky", "mastodon"]')
    names: list[str] = []
    for name in value:
        key = name.strip().lower()
        if key and key not in names:
            names.append(key)
    if required and not names:
        raise _Refused("choose at least one channel")
    return names


def serve_stdio(server: Server, reader: BinaryIO, writer: BinaryIO) -> None:
    """Answer each line ``reader`` gives, one JSON-RPC message or batch, on ``writer``, until ``reader`` ends."""
    version = PROTOCOL_VERSIONS[0]
    for line in reader:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            answer: Any = _failure(None, PARSE_ERROR, "Parse error: a line that is not JSON")
        else:
            if _is_initialize(message):
                version = negotiate(_requested_version(message))
            answer = server.respond(message, version)
        if answer is not None:
            writer.write(_encode(answer) + b"\n")
            writer.flush()


def run_stdio(server: Server) -> int:
    """Serve this process's standard input and output, until the client closes the input.

    Standard output then carries the protocol and nothing else, so whatever a channel prints, such
    as the ``stdout`` channel's posts, goes to standard error instead.
    """
    protocol, printed = sys.stdout.buffer, sys.stdout
    sys.stdout = sys.stderr
    try:
        serve_stdio(server, sys.stdin.buffer, protocol)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        sys.stdout = printed
    return 0


def _loopback(host: str) -> bool:
    """Whether every address ``host`` names is this machine's own."""
    try:
        found = socket.getaddrinfo(host.strip("[]"), None, type=socket.SOCK_STREAM)
        return bool(found) and all(ipaddress.ip_address(info[4][0]).is_loopback for info in found)
    except (OSError, ValueError):
        return False


def check_listening(host: str, token: str | None) -> None:
    """Refuse a token that could be guessed, and a host beyond this machine with no token at all."""
    if token is not None:
        if len(token) < SHORTEST_TOKEN:
            raise ValueError(
                f"{TOKEN_VARIABLE} is {len(token)} characters, which could be guessed: use at least "
                f'{SHORTEST_TOKEN}, such as python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        if not (token.isascii() and token.isprintable()) or " " in token:
            raise ValueError(f"{TOKEN_VARIABLE} must be letters, digits and punctuation, without spaces")
    elif not _loopback(host):
        raise ValueError(
            f"listening on {host} lets other machines reach the server, so it needs a token: set {TOKEN_VARIABLE} "
            "to a long random value, and have each client send it as Authorization: Bearer <token>"
        )


def _bearer_matches(header: str | None, token: str) -> bool:
    scheme, _, value = (header or "").strip().partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode("utf-8"), token.encode("utf-8"))


def _accepts_json(accept: str | None) -> bool:
    if accept is None:
        return True
    types = {part.split(";")[0].strip().lower() for part in accept.split(",")}
    return bool(types & {"application/json", "application/*", "*/*"})


class HttpServer(ThreadingHTTPServer):
    """Herald's MCP endpoint over HTTP, at :data:`ENDPOINT`. Each request runs in its own thread."""

    daemon_threads = True
    #: On Windows, reusing an address lets another program bind the same port and take requests,
    #: tokens and all, so it is left off there.
    allow_reuse_address = os.name != "nt"

    def __init__(self, server: Server, host: str, port: int, token: str | None) -> None:
        check_listening(host, token)
        family = socket.getaddrinfo(host.strip("[]"), port, type=socket.SOCK_STREAM)[0][0]
        self.address_family = family
        self.mcp = server
        self.token = token
        super().__init__((host.strip("[]"), port), _Handler)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        shown = f"[{host}]" if ":" in str(host) else str(host)
        return f"http://{shown}:{port}{ENDPOINT}"


class _Handler(BaseHTTPRequestHandler):
    server: HttpServer
    server_version = "herald"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    #: Seconds a connection may sit without sending, so an idle one cannot hold a thread for ever.
    timeout = 60
    #: Whether this response is one every client meets in passing, which the log leaves out.
    expected = False
    #: Whether this request's body has been read.
    body_read = False

    def do_POST(self) -> None:
        if not self._admitted():
            return
        if self.headers.get_content_type() != "application/json":
            self._refuse(415, "send the message as application/json")
            return
        if not _accepts_json(self.headers.get("Accept")):
            self._refuse(406, "this server answers in application/json, which the Accept header rules out")
            return
        length = (self.headers.get("Content-Length") or "").strip()
        if not length.isdigit():
            self._refuse(411, "a request needs its Content-Length")
            return
        if int(length) > MAX_REQUEST:
            self._refuse(413, f"a request may be at most {MAX_REQUEST} bytes")
            return
        body = self.rfile.read(int(length))
        self.body_read = True
        try:
            message = json.loads(body)
        except ValueError:
            self._send(400, _failure(None, PARSE_ERROR, "Parse error: the body is not JSON"))
            return
        version = self._version(message)
        if version is None:
            return
        answer = self.server.mcp.respond(message, version)
        if answer is None:
            self._send(202, None)
        else:
            self._send(200 if isinstance(message, list) or _is_request(message) else 400, answer)

    def do_GET(self) -> None:
        if self._admitted():
            # Clients ask for an event stream after the handshake, and a 405 is how they learn there is none.
            self.expected = True
            self._refuse(405, "this server opens no event stream: send each message with POST", {"Allow": "POST"})

    def do_DELETE(self) -> None:
        if self._admitted():
            self._refuse(405, "this server keeps no session to end", {"Allow": "POST"})

    def _admitted(self) -> bool:
        """Whether the request may go on: to the endpoint, from no browser page, with the token if one is set."""
        self.expected = self.body_read = False  # one connection can carry several requests
        if urllib.parse.urlsplit(self.path).path != ENDPOINT:
            self._refuse(404, f"the MCP endpoint is {ENDPOINT}")
            return False
        if self.headers.get("Origin") is not None:
            self._refuse(403, "a request from a browser page is refused")
            return False
        if self.server.token is not None and not _bearer_matches(self.headers.get("Authorization"), self.server.token):
            self._refuse(401, "this server needs its token, as Authorization: Bearer", {"WWW-Authenticate": "Bearer"})
            return False
        return True

    def _version(self, message: Any) -> str | None:
        """The revision to answer ``message`` in, or ``None`` after refusing one this server does not speak.

        An ``initialize`` request names its revision in its body. Every later request names it in
        the ``MCP-Protocol-Version`` header, and a request without the header is taken to speak
        :data:`UNNAMED_VERSION`. A revision this server does not speak gets ``400`` and an error that
        no 2026-07-28 server would give, which tells a client of that revision to fall back.
        """
        if _is_initialize(message):
            return negotiate(_requested_version(message))
        named = self.headers.get("MCP-Protocol-Version")
        if named is None:
            return UNNAMED_VERSION
        if named.strip() in PROTOCOL_VERSIONS:
            return named.strip()
        request_id = message.get("id") if _is_request(message) else None
        if isinstance(request_id, bool) or not isinstance(request_id, str | int | float):
            request_id = None
        spoken = ", ".join(PROTOCOL_VERSIONS)
        reason = (
            f"Invalid request: this server does not speak MCP-Protocol-Version {named.strip()!r}. It speaks {spoken}"
        )
        # Every client of 2026-07-28 meets this once, on its first request, before it falls back.
        self.expected = True
        self._send(400, _failure(request_id, INVALID_REQUEST, f"{reason}, opened with an initialize request"))
        return None

    def _refuse(self, status: int, reason: str, headers: Mapping[str, str] | None = None) -> None:
        self._send(status, _failure(None, INVALID_REQUEST, reason), headers)

    def _send(self, status: int, message: Any, headers: Mapping[str, str] | None = None) -> None:
        body = b"" if message is None else _encode(message)
        self.send_response(status)
        if message is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if status >= 400:
            self._discard_body()
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _discard_body(self) -> None:
        """Read a refused request's body, if it is no bigger than a request may be, and drop it.

        Closing a connection with a request's bytes still unread makes the system reset it, and a
        reset can reach the client before the refusal does, which then reads as a dropped
        connection rather than as, say, a wrong token.
        """
        length = (self.headers.get("Content-Length") or "").strip()
        if not self.body_read and length.isdigit() and int(length) <= MAX_REQUEST:
            self.rfile.read(int(length))
        self.body_read = True

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """Log a request only when it was refused or failed, and not in a way every client meets."""
        if str(code).isdigit() and int(code) >= 400 and not self.expected:
            super().log_request(code, size)


def run_http(server: Server, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, token: str | None = None) -> int:
    """Serve over HTTP until interrupted. Refuses, before listening, to go beyond this machine without a token."""
    http_server = HttpServer(server, host, port, token)
    guarded = f" Each request must carry {TOKEN_VARIABLE} as Authorization: Bearer." if token else ""
    print(f"Herald's MCP server is listening at {http_server.url}.{guarded}", file=sys.stderr, flush=True)
    try:
        http_server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        http_server.server_close()
    return 0


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ENDPOINT",
    "INSTRUCTIONS",
    "PROTOCOL_VERSIONS",
    "TOKEN_VARIABLE",
    "HttpServer",
    "Server",
    "check_listening",
    "negotiate",
    "run_http",
    "run_stdio",
    "serve_stdio",
]
