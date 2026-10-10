"""Mastodon: a status posted with an access token, on whichever server the account lives.

Mastodon counts graphemes, and every link as 23 characters, and takes 500 unless its server says
otherwise. A preview cannot ask the server, so ``HERALD_MASTODON_MAX_CHARACTERS`` sets the limit,
``herald check`` reads the server's own and says when the two differ, and the server refuses
anything longer, which Herald records as not posted. Each post carries an Idempotency-Key, which
Mastodon honours for an hour, so the same text sent twice within the hour is posted once.

Create the token under Preferences, Development, with the ``write:statuses`` scope, and the
``read:accounts`` scope for ``herald check``. ``HERALD_MASTODON_VISIBILITY`` may be public (the
default), unlisted, private or direct.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping

from herald import http
from herald.channels import Channel, CheckResult, NotPosted, PostResult
from herald.counting import graphemes, with_links_as

DEFAULT_LIMIT = 500
LINK_WEIGHT = 23
VISIBILITIES = ("public", "unlisted", "private", "direct")


class MastodonChannel(Channel):
    name = "mastodon"
    label = "Mastodon"
    limit = DEFAULT_LIMIT
    credentials = ("HERALD_MASTODON_SERVER", "HERALD_MASTODON_TOKEN")

    def __init__(self, max_characters: int | None = None) -> None:
        if max_characters is None:
            configured = os.environ.get("HERALD_MASTODON_MAX_CHARACTERS", "").strip()
            max_characters = int(configured) if configured.isdigit() and int(configured) > 0 else DEFAULT_LIMIT
        self.limit = max_characters

    def count(self, text: str) -> int:
        return graphemes(with_links_as(text, LINK_WEIGHT))

    def _server(self, env: Mapping[str, str]) -> str:
        server = self.setting(env, "HERALD_MASTODON_SERVER").rstrip("/")
        return server if "://" in server else f"https://{server}"

    def _token(self, env: Mapping[str, str]) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.setting(env, 'HERALD_MASTODON_TOKEN')}"}

    def check(self, env: Mapping[str, str]) -> CheckResult:
        missing = self.missing_credentials(env)
        if missing:
            return CheckResult(False, "set " + ", ".join(missing))
        server = self._server(env)
        try:
            account = http.get(f"{server}/api/v1/accounts/verify_credentials", headers=self._token(env))
            instance = http.get(f"{server}/api/v2/instance")
        except Exception as error:
            return CheckResult(False, f"could not reach {http.where(server)}: {type(error).__name__}: {error}")
        if not account.ok:
            return CheckResult(False, f"the server refused the token ({account.status}: {http.detail(account)})")
        who = f"@{(account.json() or {}).get('acct', '?')}"
        statuses = ((instance.json() or {}).get("configuration") or {}).get("statuses") or {}
        allowed = statuses.get("max_characters")
        if not isinstance(allowed, int) or allowed <= 0:
            return CheckResult(True, f"posting as {who}; the server did not say its limit, so {self.limit} is used")
        if allowed < self.limit:
            return CheckResult(
                False,
                f"posting as {who}, but the server takes {allowed} characters, not {self.limit}: "
                f"set HERALD_MASTODON_MAX_CHARACTERS={allowed}",
            )
        if allowed > self.limit:
            return CheckResult(
                True,
                f"posting as {who}; the server takes {allowed} characters, and posts are held to {self.limit} "
                f"until HERALD_MASTODON_MAX_CHARACTERS={allowed} is set",
            )
        return CheckResult(True, f"posting as {who}, {allowed} characters a post")

    def post(self, text: str, env: Mapping[str, str]) -> PostResult:
        visibility = self.setting(env, "HERALD_MASTODON_VISIBILITY", "public")
        if visibility not in VISIBILITIES:
            raise NotPosted(f"HERALD_MASTODON_VISIBILITY must be one of {', '.join(VISIBILITIES)}, not {visibility!r}")
        server = self._server(env)
        key = hashlib.sha256(f"{server}\n{text}".encode()).hexdigest()[:40]
        answer = http.post_json(
            f"{server}/api/v1/statuses",
            {"status": text, "visibility": visibility},
            headers={**self._token(env), "Idempotency-Key": key},
        )
        http.judge(answer, "Mastodon")
        status = answer.json() or {}
        return PostResult(url=str(status.get("url") or status.get("uri") or ""), id=str(status.get("id") or ""))


__all__ = ["MastodonChannel"]
