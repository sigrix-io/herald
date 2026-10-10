"""Recorded HTTP exchanges, replayed in order, so each channel is tested with no network.

An exchange file under ``tests/exchanges/`` lists what a channel sends and what the platform
answers, written from the platform's own documentation of each call; its ``note`` says which.
A test replays a file through ``herald.http.transport``. Each request must match the next
exchange: its method, its address, every header the exchange names and its body. A request
with no exchange left fails the test, and so does an exchange no request used.

In an exchange, ``{NAME}`` stands for the test's value of the variable NAME (and
``{HERALD_VERSION}`` for Herald's version), and the string ``"*"`` matches any value.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import herald
from herald.http import Request, Response

EXCHANGES = Path(__file__).resolve().parent / "exchanges"

#: What a recorded answer may raise instead of answering: failures after the request was sent.
_RAISED = {"TimeoutError": TimeoutError, "ConnectionResetError": ConnectionResetError}


def _fill(value: Any, env: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        for name, setting in {**env, "HERALD_VERSION": herald.__version__}.items():
            value = value.replace("{" + name + "}", setting)
        return value
    if isinstance(value, dict):
        return {key: _fill(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill(item, env) for item in value]
    return value


def _matches(expected: Any, actual: Any) -> bool:
    if expected == "*":
        return True
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and expected.keys() == actual.keys()
            and all(_matches(expected[key], actual[key]) for key in expected)
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(_matches(want, got) for want, got in zip(expected, actual, strict=True))
        )
    return expected == actual


class Replay:
    """Answers each request with the next recorded exchange, after checking the request matches it."""

    def __init__(self, name: str, env: Mapping[str, str]) -> None:
        self.name = name
        self.env = dict(env)
        self.pending: list[dict[str, Any]] = json.loads((EXCHANGES / f"{name}.json").read_text(encoding="utf-8"))
        self.sent: list[Request] = []

    def __call__(self, request: Request, timeout: float) -> Response:
        self.sent.append(request)
        assert self.pending, f"{self.name}: no exchange left for {request.method} {request.url}"
        exchange = self.pending.pop(0)
        want = _fill(exchange["request"], self.env)
        assert (request.method, request.url) == (want["method"], want["url"]), f"{self.name}: {exchange['note']}"
        headers = {name.lower(): value for name, value in request.headers.items()}
        for name, value in want.get("headers", {}).items():
            assert _matches(value, headers.get(name.lower())), (
                f"{self.name}: header {name} was {headers.get(name.lower())!r}"
            )
        if "json" in want:
            assert headers.get("content-type") == "application/json"
            sent = json.loads(request.body or b"null")
            assert _matches(want["json"], sent), f"{self.name}: sent {json.dumps(sent, ensure_ascii=False)}"
        elif "form" in want:
            sent = dict(urllib.parse.parse_qsl((request.body or b"").decode("ascii")))
            assert _matches(want["form"], sent), f"{self.name}: sent {sent}"
        else:
            assert not request.body, f"{self.name}: a {request.method} with a body"
        answer = _fill(exchange["response"], self.env)
        if "raise" in answer:
            raise _RAISED[answer["raise"]](answer.get("message", ""))
        if "json" in answer:
            body = json.dumps(answer["json"]).encode("utf-8")
        else:
            body = answer.get("text", "").encode("utf-8")
        return Response(
            answer["status"], {name.lower(): value for name, value in answer.get("headers", {}).items()}, body
        )
