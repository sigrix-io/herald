r"""LinkedIn: a post on a member's profile, through the self-serve "Share on LinkedIn" product.

A member's token lasts 60 days and cannot be refreshed, so ``herald auth linkedin`` signs in
again from this machine, and ``herald check`` warns two weeks before the token lapses. The app
needs two products, "Share on LinkedIn" and "Sign In with LinkedIn using OpenID Connect", and
``http://localhost:8765/callback`` among its redirect URLs.

LinkedIn reads a post's text as "little text", in which \ | { } @ [ ] ( ) < > # * _ ~ are markup
and the text after an unescaped one can be lost. They are escaped when the post is sent, so the
post reads as written, and counted as escaped, in UTF-16 code units.
"""

from __future__ import annotations

import secrets
import time
import urllib.parse
import webbrowser
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from herald import http
from herald.channels import Channel, CheckResult, NotPosted, PostResult, SignIn
from herald.counting import utf16_length

API = "https://api.linkedin.com"
OAUTH = "https://www.linkedin.com/oauth/v2"
#: The Posts API version, as YYYYMM. LinkedIn supports each version for a year.
DEFAULT_VERSION = "202607"
SCOPES = "openid profile w_member_social"
DEFAULT_PORT = 8765
WARN_DAYS = 14
_RESERVED = frozenset("\\|{}@[]()<>#*_~")


def escape(text: str) -> str:
    """``text`` with LinkedIn's little-text markup characters escaped."""
    return "".join(f"\\{char}" if char in _RESERVED else char for char in text)


def publish(author: str, text: str, token: str, version: str) -> PostResult:
    """Post ``text`` as ``author``, a person's URN or an organisation's, through the Posts API."""
    answer = http.post_json(
        f"{API}/rest/posts",
        {
            "author": author,
            "commentary": escape(text),
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        },
        headers={
            "Authorization": f"Bearer {token}",
            "LinkedIn-Version": version,
            "X-Restli-Protocol-Version": "2.0.0",
        },
    )
    http.judge(answer, "LinkedIn")
    urn = answer.headers.get("x-restli-id", "")
    return PostResult(url=f"https://www.linkedin.com/feed/update/{urn}/" if urn else "", id=urn)


def browser_sign_in(
    client: str,
    scopes: str,
    *,
    port: int,
    open_browser: Callable[[str], object],
    say: Callable[[str], object],
    wait: float,
) -> tuple[str, str]:
    """Send the browser to LinkedIn's sign-in and wait for it to come back, once, for ``wait`` seconds.

    LinkedIn sends the browser back to ``http://localhost:PORT/callback``, where a server on the
    loopback address waits for it. Returns the authorization code and the address it came back to,
    which exchanging the code needs.
    """
    state = secrets.token_urlsafe(24)
    received: dict[str, str] = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parts = urllib.parse.urlsplit(self.path)
            if parts.path != "/callback":
                self.send_error(404)
                return
            received.update(urllib.parse.parse_qsl(parts.query))
            page = b"<!doctype html><title>Herald</title><p>Done. You can close this tab and go back to the terminal."
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *args: Any) -> None:
            """Say nothing: the request line holds the sign-in code."""

    with HTTPServer(("127.0.0.1", port), Callback) as server:
        redirect = f"http://localhost:{server.server_port}/callback"
        query = {
            "response_type": "code",
            "client_id": client,
            "redirect_uri": redirect,
            "state": state,
            "scope": scopes,
        }
        address = f"{OAUTH}/authorization?{urllib.parse.urlencode(query)}"
        say(f"Sign in to LinkedIn in the browser. If none opens, visit:\n{address}")
        open_browser(address)
        server.timeout = 0.5
        deadline = time.monotonic() + wait
        while not received and time.monotonic() < deadline:
            server.handle_request()
    if not received:
        raise TimeoutError(f"no sign-in came back within {wait:.0f} seconds")
    if not secrets.compare_digest(received.get("state", ""), state):
        raise ValueError("the sign-in came back for another request, so it was ignored")
    if "error" in received:
        raise ValueError(f"LinkedIn did not sign you in: {received.get('error_description') or received['error']}")
    return received.get("code", ""), redirect


def exchange_code(code: str, redirect: str, client: str, secret: str) -> dict[str, Any]:
    """LinkedIn's grant for a sign-in's code: the access token, its lifetime, and any refresh token."""
    answer = http.post_form(
        f"{OAUTH}/accessToken",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "client_id": client,
            "client_secret": secret,
        },
    )
    if not answer.ok:
        raise ValueError(f"LinkedIn refused the sign-in ({answer.status}: {http.detail(answer)})")
    grant = answer.json() or {}
    if not str(grant.get("access_token") or ""):
        raise ValueError("LinkedIn's answer to the sign-in held no token")
    return grant


class LinkedInChannel(Channel):
    name = "linkedin"
    label = "LinkedIn profile"
    limit = 3000
    credentials = ("HERALD_LINKEDIN_TOKEN",)

    def count(self, text: str) -> int:
        return utf16_length(escape(text))

    def _bearer(self, env: Mapping[str, str], token: str = "") -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.setting(env, 'HERALD_LINKEDIN_TOKEN')}"}

    def _author(self, env: Mapping[str, str]) -> str:
        stated = self.setting(env, "HERALD_LINKEDIN_AUTHOR")
        if stated:
            return stated
        answer = http.before_posting(
            "Reading the LinkedIn member", lambda: http.get(f"{API}/v2/userinfo", headers=self._bearer(env))
        )
        if not answer.ok:
            raise NotPosted(
                f"LinkedIn refused the token ({answer.status}: {http.detail(answer)}); run herald auth linkedin"
            )
        member = (answer.json() or {}).get("sub")
        if not member:
            raise NotPosted("LinkedIn's answer about the member held no id")
        return f"urn:li:person:{member}"

    def post(self, text: str, env: Mapping[str, str]) -> PostResult:
        author = self._author(env)
        version = self.setting(env, "HERALD_LINKEDIN_VERSION", DEFAULT_VERSION)
        return publish(author, text, self.setting(env, "HERALD_LINKEDIN_TOKEN"), version)

    def _expires(self, env: Mapping[str, str]) -> datetime | None:
        """When the token lapses: as ``herald auth linkedin`` recorded it, else as LinkedIn says."""
        stated = self.setting(env, "HERALD_LINKEDIN_TOKEN_EXPIRES")
        if stated:
            try:
                moment = datetime.fromisoformat(stated.replace("Z", "+00:00"))
            except ValueError:
                return None
            return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        client, secret = (
            self.setting(env, "HERALD_LINKEDIN_CLIENT_ID"),
            self.setting(env, "HERALD_LINKEDIN_CLIENT_SECRET"),
        )
        if not (client and secret):
            return None
        try:
            answer = http.post_form(
                f"{OAUTH}/introspectToken",
                {"client_id": client, "client_secret": secret, "token": self.setting(env, "HERALD_LINKEDIN_TOKEN")},
            )
        except Exception:
            return None
        expires_at = (answer.json() or {}).get("expires_at") if answer.ok else None
        return datetime.fromtimestamp(expires_at, UTC) if isinstance(expires_at, int) else None

    def check(self, env: Mapping[str, str]) -> CheckResult:
        missing = self.missing_credentials(env)
        if missing:
            return CheckResult(False, "set " + ", ".join(missing) + ", which herald auth linkedin gives")
        try:
            answer = http.get(f"{API}/v2/userinfo", headers=self._bearer(env))
        except Exception as error:
            return CheckResult(False, f"could not reach LinkedIn: {type(error).__name__}: {error}")
        if answer.status == 401:
            return CheckResult(
                False, "LinkedIn refused the token, which lapses after 60 days: run herald auth linkedin"
            )
        if not answer.ok:
            return CheckResult(False, f"LinkedIn answered {answer.status} ({http.detail(answer)})")
        who = (answer.json() or {}).get("name") or "the member"
        expires = self._expires(env)
        if expires is None:
            return CheckResult(
                True,
                f"posting as {who}; set HERALD_LINKEDIN_TOKEN_EXPIRES, which herald auth linkedin gives, "
                "to be warned before the token lapses",
            )
        days = (expires - datetime.now(UTC)).days
        if days < 0:
            return CheckResult(False, f"the token lapsed on {expires:%Y-%m-%d}: run herald auth linkedin")
        if days <= WARN_DAYS:
            return CheckResult(
                True,
                f"posting as {who}, but the token lapses on {expires:%Y-%m-%d}, in {days} days: "
                "run herald auth linkedin before then",
            )
        return CheckResult(True, f"posting as {who} until {expires:%Y-%m-%d}")

    def sign_in(
        self,
        env: Mapping[str, str],
        *,
        port: int = DEFAULT_PORT,
        open_browser: Callable[[str], object] = webbrowser.open,
        say: Callable[[str], object] = print,
        wait: float = 300.0,
    ) -> SignIn:
        """Sign in through the browser and return the token, with the member and when it lapses.

        LinkedIn sends the browser back to ``http://localhost:PORT/callback``, where a server on
        the loopback address waits for it, once, for ``wait`` seconds.
        """

        client, secret = (
            self.setting(env, "HERALD_LINKEDIN_CLIENT_ID"),
            self.setting(env, "HERALD_LINKEDIN_CLIENT_SECRET"),
        )
        if not (client and secret):
            raise ValueError("set HERALD_LINKEDIN_CLIENT_ID and HERALD_LINKEDIN_CLIENT_SECRET, from the app's Auth tab")
        code, redirect = browser_sign_in(client, SCOPES, port=port, open_browser=open_browser, say=say, wait=wait)
        grant = exchange_code(code, redirect, client, secret)
        token = str(grant["access_token"])
        member_answer = http.get(f"{API}/v2/userinfo", headers=self._bearer(env, token))
        member = (member_answer.json() or {}) if member_answer.ok else {}
        settings = {"HERALD_LINKEDIN_TOKEN": token}
        if member.get("sub"):
            settings["HERALD_LINKEDIN_AUTHOR"] = f"urn:li:person:{member['sub']}"
        summary = f"Signed in as {member.get('name') or 'a LinkedIn member'}."
        lifetime = grant.get("expires_in")
        if isinstance(lifetime, int) and lifetime > 0:
            expires = (datetime.now(UTC) + timedelta(seconds=lifetime)).replace(microsecond=0)
            settings["HERALD_LINKEDIN_TOKEN_EXPIRES"] = expires.isoformat()
            summary += f" The token lasts until {expires:%Y-%m-%d}; sign in again before then."
        return SignIn(settings=settings, secret="HERALD_LINKEDIN_TOKEN", summary=summary)  # noqa: S106 - a name


__all__ = ["LinkedInChannel", "browser_sign_in", "escape", "exchange_code", "publish"]
