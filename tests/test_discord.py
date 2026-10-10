"""Discord, against recorded exchanges: a webhook found first, then a message that pings nobody."""

from __future__ import annotations

import pytest

from herald import NotPosted
from herald.channels.discord import DiscordChannel

HOOK = "https://discord.com/api/webhooks/1234567890/discord-webhook-token-abcdef"
ENV = {"HERALD_DISCORD_WEBHOOK_URL": HOOK}
TEXT = "Version 2.0 is out, @everyone: https://example.com/releases/2.0"


def test_a_message_is_posted_and_linked_and_pings_nobody(replay) -> None:
    replay("discord/post", ENV)
    result = DiscordChannel().post(TEXT, ENV)
    assert result.url == "https://discord.com/channels/197038439483310086/199737254929760256/1287654321098765432"
    assert result.id == "1287654321098765432"


def test_a_webhook_that_posts_into_a_thread_keeps_its_query(replay) -> None:
    replay("discord/post_in_thread", ENV)
    DiscordChannel().post(TEXT, {"HERALD_DISCORD_WEBHOOK_URL": f"{HOOK}?thread_id=555"})


def test_a_deleted_webhook_is_not_posted_to(replay) -> None:
    replayed = replay("discord/unknown_webhook", ENV)
    with pytest.raises(NotPosted, match=r"Discord did not find the webhook \(404: Unknown Webhook\)"):
        DiscordChannel().post(TEXT, ENV)
    assert [request.method for request in replayed.sent] == ["GET"]


def test_a_rate_limited_message_is_not_posted(replay) -> None:
    replay("discord/rate_limited", ENV)
    with pytest.raises(NotPosted, match=r"Discord refused the post \(429: You are being rate limited\.\)"):
        DiscordChannel().post(TEXT, ENV)


def test_check_names_the_webhook_and_not_its_token(replay) -> None:
    replay("discord/check", ENV)
    result = DiscordChannel().check(ENV)
    assert (result.ok, result.detail) == (True, "posts as Herald to channel 199737254929760256")
    assert "discord-webhook-token" not in result.detail


def test_discord_counts_utf16_code_units() -> None:
    assert DiscordChannel().count("ok " + chr(0x1F680)) == 5, "a rocket is two units"
