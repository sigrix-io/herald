"""HTTP for the channels, with the standard library, and what a failure says about a post.

A channel makes its requests through :func:`send`. Whether a failed post may be tried again
depends on whether the platform may have it, so failures come in two kinds:

* The request never fully left: the name did not resolve, the connection was refused, TLS
  failed, or the connection broke while the request was being written. :class:`Unsent` says so.
  It is a :class:`~herald.channels.NotPosted`, so Herald records the post as failed and a later
  run may try again.
* Anything after the request was written may have reached the platform: a timeout waiting for
  the answer, a connection dropped before it. That propagates as it is, and Herald leaves the
  post for a person to check.

An answer comes back as a :class:`Response` whatever its status, and :func:`judge` says what a
post's answer means: 2xx posted, 4xx refused (:class:`~herald.channels.NotPosted`), anything else
:class:`Uncertain`. Redirects are not followed, since an API does not redirect a post and
following one would send it somewhere it was not addressed. Only ``https`` addresses are used,
so a credential never crosses the network in the clear, and a message names a host, never a
path, because a path can carry a token.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from herald.channels import NotPosted

#: The most of an answer that is read. Platforms answer a post in a few kilobytes.
MAX_ANSWER = 1 << 20

#: How long to wait for a platform, in seconds.
TIMEOUT = 30.0


class Unsent(NotPosted):
    """The request never fully reached the platform, so nothing was posted."""


class Uncertain(Exception):
    """The platform's answer leaves it unknown whether the post was made."""


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None


@dataclass(frozen=True)
class Response:
    status: int
    #: Header names in lower case.
    headers: Mapping[str, str]
    body: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        """The body as JSON, or ``None`` when it is empty or not JSON."""
        try:
            return json.loads(self.body) if self.body.strip() else None
        except ValueError:
            return None


def where(url: str) -> str:
    """The scheme and host of ``url``, which is all a message may say about it."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}" if parts.hostname else "an address with no host"


def detail(answer: Response) -> str:
    """What the platform said, in a line: its error message if it gave one, else its text."""
    data = answer.json()
    if isinstance(data, dict):
        for key in ("message", "error_description", "description", "error"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:300]
    return " ".join(answer.text.split())[:300]


def judge(answer: Response, platform: str) -> None:
    """Return if ``answer`` says the post was made; raise what it says otherwise."""
    if answer.ok:
        return
    said = detail(answer)
    reason = f"{answer.status}: {said}" if said else str(answer.status)
    if 400 <= answer.status < 500:
        raise NotPosted(f"{platform} refused the post ({reason})")
    raise Uncertain(f"{platform} answered {reason}, so the post may exist")


def before_posting(what: str, request: Callable[[], Response]) -> Response:
    """Make a request that comes before the post. Whatever goes wrong, nothing was posted."""
    try:
        return request()
    except NotPosted:
        raise
    except Exception as error:
        raise NotPosted(f"{what} failed, before anything was posted: {type(error).__name__}: {error}") from None


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirects())


def _lower(headers: Any) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in (headers.items() if headers else ())}


def _urllib(request: Request, timeout: float) -> Response:
    outgoing = urllib.request.Request(  # noqa: S310 - send() lets only https through
        request.url, data=request.body, headers=dict(request.headers), method=request.method
    )
    try:
        with _OPENER.open(outgoing, timeout=timeout) as answer:
            return Response(answer.status, _lower(answer.headers), answer.read(MAX_ANSWER))
    except urllib.error.HTTPError as answer:
        with answer:
            return Response(answer.code, _lower(answer.headers), answer.read(MAX_ANSWER))
    except urllib.error.URLError as error:
        # urllib raises URLError only while it connects and writes the request; a failure
        # after that, waiting for the answer, comes through as itself.
        raise Unsent(f"could not reach {where(request.url)}: {error.reason}") from None


#: What sends a request. Tests replace it with recorded exchanges.
transport: Callable[[Request, float], Response] = _urllib


def send(request: Request, *, timeout: float = TIMEOUT) -> Response:
    """Send ``request`` over https and return the platform's answer, whatever its status."""
    from herald import __version__

    parts = urllib.parse.urlsplit(request.url)
    if parts.scheme != "https" or not parts.hostname:
        raise Unsent(f"refusing {where(request.url)}: Herald only sends to https addresses")
    headers = {"User-Agent": f"herald-post/{__version__}", "Accept": "application/json", **request.headers}
    for name, value in headers.items():
        if "\r" in value or "\n" in value:
            raise Unsent(f"the {name} header holds a line break, so the request was not sent")
    return transport(Request(request.method, request.url, headers, request.body), timeout)


def get(url: str, *, headers: Mapping[str, str] | None = None) -> Response:
    return send(Request("GET", url, dict(headers or {})))


def post_json(url: str, payload: object, *, headers: Mapping[str, str] | None = None) -> Response:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return send(Request("POST", url, {"Content-Type": "application/json", **(headers or {})}, body))


def post_form(url: str, fields: Mapping[str, str], *, headers: Mapping[str, str] | None = None) -> Response:
    body = urllib.parse.urlencode(fields).encode("ascii")
    return send(Request("POST", url, {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}, body))


__all__ = [
    "MAX_ANSWER",
    "TIMEOUT",
    "Request",
    "Response",
    "Uncertain",
    "Unsent",
    "before_posting",
    "detail",
    "get",
    "judge",
    "post_form",
    "post_json",
    "send",
    "transport",
    "where",
]
