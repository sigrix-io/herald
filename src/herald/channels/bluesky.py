"""Bluesky: an app password signs in, and the post is a record in the account's repository.

Bluesky takes 300 graphemes and 3,000 bytes of UTF-8. A link in the text is a link only when the
post carries a facet saying where it is, in bytes, so the channel adds one for each link. Sign in
with an app password (Settings, Privacy and security, App passwords), never the account's own.
``HERALD_BLUESKY_SERVICE`` names the server to sign in at, for an account Bluesky does not host.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from herald import http
from herald.channels import Channel, CheckResult, NotPosted, PostResult
from herald.counting import LINK, graphemes

DEFAULT_SERVICE = "https://bsky.social"
MAX_BYTES = 3000


def link_facets(text: str) -> list[dict[str, Any]]:
    """A link facet for each link in ``text``, at its UTF-8 byte offsets."""
    facets = []
    for match in LINK.finditer(text):
        start = len(text[: match.start()].encode("utf-8"))
        facets.append(
            {
                "index": {"byteStart": start, "byteEnd": start + len(match.group().encode("utf-8"))},
                "features": [{"$type": "app.bsky.richtext.facet#link", "uri": match.group()}],
            }
        )
    return facets


class BlueskyChannel(Channel):
    name = "bluesky"
    label = "Bluesky"
    limit = 300
    credentials = ("HERALD_BLUESKY_HANDLE", "HERALD_BLUESKY_APP_PASSWORD")

    def count(self, text: str) -> int:
        # Long emoji sequences can run out of bytes before graphemes, so every ten bytes also
        # count as a character, and the count is the larger of the two.
        per_character = MAX_BYTES // 300
        return max(graphemes(text), -(-len(text.encode("utf-8")) // per_character))

    def _service(self, env: Mapping[str, str]) -> str:
        return self.setting(env, "HERALD_BLUESKY_SERVICE", DEFAULT_SERVICE).rstrip("/")

    def _sign_in(self, env: Mapping[str, str]) -> dict[str, Any]:
        address = f"{self._service(env)}/xrpc/com.atproto.server.createSession"
        identity = {
            "identifier": self.setting(env, "HERALD_BLUESKY_HANDLE").lstrip("@"),
            "password": self.setting(env, "HERALD_BLUESKY_APP_PASSWORD"),
        }
        answer = http.before_posting("Signing in to Bluesky", lambda: http.post_json(address, identity))
        if not answer.ok:
            raise NotPosted(f"Bluesky refused the sign-in ({answer.status}: {http.detail(answer)})")
        session = answer.json()
        if not isinstance(session, dict) or not session.get("accessJwt") or not session.get("did"):
            raise NotPosted("Bluesky's answer to the sign-in held no session")
        return session

    def check(self, env: Mapping[str, str]) -> CheckResult:
        missing = self.missing_credentials(env)
        if missing:
            return CheckResult(False, "set " + ", ".join(missing))
        try:
            session = self._sign_in(env)
        except NotPosted as refusal:
            return CheckResult(False, str(refusal))
        return CheckResult(True, f"signed in as @{session.get('handle') or session['did']}")

    def post(self, text: str, env: Mapping[str, str]) -> PostResult:
        session = self._sign_in(env)
        record: dict[str, Any] = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        facets = link_facets(text)
        if facets:
            record["facets"] = facets
        answer = http.post_json(
            f"{self._service(env)}/xrpc/com.atproto.repo.createRecord",
            {"repo": session["did"], "collection": "app.bsky.feed.post", "record": record},
            headers={"Authorization": f"Bearer {session['accessJwt']}"},
        )
        http.judge(answer, "Bluesky")
        uri = str((answer.json() or {}).get("uri") or "")
        key = uri.rsplit("/", 1)[-1] if uri.startswith("at://") else ""
        profile = session.get("handle") or session["did"]
        return PostResult(url=f"https://bsky.app/profile/{profile}/post/{key}" if key else "", id=uri)


__all__ = ["BlueskyChannel", "link_facets"]
