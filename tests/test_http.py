"""The HTTP layer: what each failure says about a post, held against a real server on this machine.

The transport tests talk plain HTTP to a server on the loopback address, which ``send`` itself
would refuse; ``send`` is tested with a stand-in transport.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from herald import NotPosted, http


class _Handler(BaseHTTPRequestHandler):
    seen: ClassVar[list[str]] = []

    def do_POST(self) -> None:
        self.seen.append(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/refused":
            body = b'{"message": "no such thing"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/silent":
            threading.Event().wait(2)  # read the request, then never answer in time
        else:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    _Handler.seen = []
    with HTTPServer(("127.0.0.1", 0), _Handler) as running:
        thread = threading.Thread(target=running.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{running.server_port}"
        running.shutdown()


def _post(address: str, timeout: float = 5.0) -> http.Response:
    return http._urllib(http.Request("POST", address, {"Content-Type": "application/json"}, b"{}"), timeout)


def test_a_redirect_is_answered_and_not_followed(server: str) -> None:
    answer = _post(f"{server}/redirect")
    assert (answer.status, answer.headers["location"]) == (302, "/elsewhere")
    assert _Handler.seen == ["/redirect"], "the post went nowhere else"


def test_a_refusal_is_an_answer_not_an_exception(server: str) -> None:
    answer = _post(f"{server}/refused")
    assert (answer.status, answer.json(), http.detail(answer)) == (404, {"message": "no such thing"}, "no such thing")


def test_a_connection_refused_is_unsent() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # bound, never listening: nothing answers there
        with pytest.raises(http.Unsent, match=r"could not reach http://127\.0\.0\.1"):
            _post(f"http://127.0.0.1:{port}/")


def test_no_answer_after_the_request_went_is_not_unsent(server: str) -> None:
    """The platform may have the post, so this must not read as safe to retry."""
    with pytest.raises(TimeoutError) as waited:
        _post(f"{server}/silent", timeout=0.3)
    assert not isinstance(waited.value, NotPosted)
    assert _Handler.seen == ["/silent"]


def test_send_uses_https_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "transport", lambda request, timeout: pytest.fail("the request was sent"))
    for address in ("http://example.com/hook", "ftp://example.com/", "file:///etc/passwd", "https:///no-host"):
        with pytest.raises(http.Unsent, match="only sends to https"):
            http.post_json(address, {})


def test_send_refuses_a_header_that_would_break_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "transport", lambda request, timeout: pytest.fail("the request was sent"))
    with pytest.raises(http.Unsent, match="Authorization header holds a line break"):
        http.get("https://example.com/", headers={"Authorization": "Bearer token\nX-Evil: 1"})


def test_send_names_herald_unless_a_channel_names_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    from herald import __version__

    sent: list[http.Request] = []
    monkeypatch.setattr(http, "transport", lambda request, timeout: sent.append(request) or http.Response(200, {}, b""))
    http.get("https://example.com/")
    http.get("https://example.com/", headers={"User-Agent": "DiscordBot (x, 1)"})
    assert [request.headers["User-Agent"] for request in sent] == [f"herald-post/{__version__}", "DiscordBot (x, 1)"]


def test_a_message_names_the_host_never_the_path() -> None:
    assert http.where("https://api.telegram.org/bot123:secret/sendMessage?x=1") == "https://api.telegram.org"


@pytest.mark.parametrize(
    ("status", "raised"),
    [
        (400, NotPosted),
        (401, NotPosted),
        (422, NotPosted),
        (429, NotPosted),
        (302, http.Uncertain),
        (500, http.Uncertain),
    ],
)
def test_judge_reads_a_status_as_refused_or_uncertain(status: int, raised: type[Exception]) -> None:
    answer = http.Response(status, {}, b'{"error": "what the platform said"}')
    with pytest.raises(raised, match="what the platform said") as judged:
        http.judge(answer, "Example")
    assert raised is NotPosted or not isinstance(judged.value, NotPosted)


def test_judge_lets_a_success_through() -> None:
    assert http.judge(http.Response(201, {}, b""), "Example") is None


def test_what_a_platform_said_is_one_short_line() -> None:
    assert (
        http.detail(http.Response(500, {}, b"<html>\n  <b>Bad\n Gateway</b>\n</html>"))
        == "<html> <b>Bad Gateway</b> </html>"
    )
    assert len(http.detail(http.Response(500, {}, b"x" * 5000))) == 300


def test_a_failure_before_the_post_is_not_posted() -> None:
    def lost() -> http.Response:
        raise TimeoutError("timed out")

    with pytest.raises(NotPosted, match="Signing in failed, before anything was posted: TimeoutError: timed out"):
        http.before_posting("Signing in", lost)
