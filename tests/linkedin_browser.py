"""A browser for the LinkedIn sign-ins, which runs in another thread as a person's would.

It opens the start page the sign-in gives it, reads the address on LinkedIn the page sends it to,
and comes straight back to the callback, as LinkedIn would once the person signs in. It talks to
the loopback address directly, so no proxy in the environment is asked.
"""

from __future__ import annotations

import http.client
import threading
import urllib.parse
from collections.abc import Callable


def _get(address: str) -> http.client.HTTPResponse:
    parts = urllib.parse.urlsplit(address)
    connection = http.client.HTTPConnection("127.0.0.1", parts.port, timeout=5)
    connection.request("GET", f"{parts.path or '/'}{'?' + parts.query if parts.query else ''}")
    answer = connection.getresponse()
    answer.read()
    connection.close()
    return answer


def browser(
    code: str = "code-from-linkedin",
    *,
    state: str | None = None,
    error: str = "",
    opened: list[str] | None = None,
    sent_to: list[str] | None = None,
) -> Callable[[str], None]:
    """``open_browser`` for a sign-in. ``opened`` collects the address it was given, ``sent_to`` the
    address on LinkedIn the start page sent it to. ``state`` and ``error`` make the callback come
    back for another sign-in, or refused."""

    def visit(address: str) -> None:
        start = _get(address)
        sign_in = start.getheader("Location", "")
        if sent_to is not None:
            sent_to.append(sign_in)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(sign_in).query))
        back = {"state": state if state is not None else query["state"]}
        back.update({"error": error, "error_description": "The member cancelled"} if error else {"code": code})
        _get(f"{query['redirect_uri']}?{urllib.parse.urlencode(back)}")

    def open_browser(address: str) -> None:
        if opened is not None:
            opened.append(address)
        threading.Thread(target=visit, args=(address,), daemon=True).start()

    return open_browser
