"""Discord: a message through a channel's webhook, whose URL is the credential.

Discord takes 2,000 characters, counted here in UTF-16 code units, which never undercounts. The
message allows no mentions, so an announcement that says @everyone pings nobody. Create the
webhook under the channel's settings, Integrations, Webhooks.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from typing import Any

from herald import http
from herald.channels import Channel, CheckResult, NotPosted, PostResult
from herald.counting import utf16_length


def _user_agent() -> dict[str, str]:
    from herald import __version__

    # Discord asks clients to name themselves this way, and its edge turns some generic agents away.
    return {"User-Agent": f"DiscordBot (https://pypi.org/project/herald-post/, {__version__})"}


class DiscordChannel(Channel):
    name = "discord"
    label = "Discord"
    limit = 2000
    credentials = ("HERALD_DISCORD_WEBHOOK_URL",)

    def count(self, text: str) -> int:
        return utf16_length(text)

    def _webhook(self, env: Mapping[str, str]) -> dict[str, Any]:
        hook = self.setting(env, "HERALD_DISCORD_WEBHOOK_URL")
        answer = http.before_posting("Reading the Discord webhook", lambda: http.get(hook, headers=_user_agent()))
        if not answer.ok:
            raise NotPosted(f"Discord did not find the webhook ({answer.status}: {http.detail(answer)})")
        found = answer.json()
        return found if isinstance(found, dict) else {}

    def check(self, env: Mapping[str, str]) -> CheckResult:
        missing = self.missing_credentials(env)
        if missing:
            return CheckResult(False, "set " + ", ".join(missing))
        try:
            hook = self._webhook(env)
        except NotPosted as refusal:
            return CheckResult(False, str(refusal))
        return CheckResult(True, f"posts as {hook.get('name') or 'the webhook'} to channel {hook.get('channel_id')}")

    def post(self, text: str, env: Mapping[str, str]) -> PostResult:
        found = self._webhook(env)
        parts = urllib.parse.urlsplit(self.setting(env, "HERALD_DISCORD_WEBHOOK_URL"))
        query = urllib.parse.urlencode([*urllib.parse.parse_qsl(parts.query), ("wait", "true")])
        answer = http.post_json(
            urllib.parse.urlunsplit(parts._replace(query=query)),
            {"content": text, "allowed_mentions": {"parse": []}},
            headers=_user_agent(),
        )
        http.judge(answer, "Discord")
        message = answer.json() or {}
        guild, channel, key = found.get("guild_id"), message.get("channel_id"), message.get("id")
        url = f"https://discord.com/channels/{guild}/{channel}/{key}" if guild and channel and key else ""
        return PostResult(url=url, id=str(key or ""))


__all__ = ["DiscordChannel"]
