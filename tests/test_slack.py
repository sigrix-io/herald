"""Slack, against recorded exchanges: an incoming webhook, with Slack's three markup characters escaped."""

from __future__ import annotations

import pytest

from herald import CheckResult, NotPosted
from herald.channels.slack import SlackChannel, escape

ENV = {"HERALD_SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/T0001/B0002/slack-webhook-secret"}


def test_a_message_is_posted_with_its_markup_escaped(replay) -> None:
    replay("slack/post", ENV)
    result = SlackChannel().post("Release 2.0 <today> & notes: https://example.com/releases/2.0", ENV)
    assert (result.url, result.id) == ("", ""), "an incoming webhook says nothing about the message it posted"


def test_escaping_covers_what_slack_reads_as_markup() -> None:
    assert escape("<!channel> & <@U123>") == "&lt;!channel&gt; &amp; &lt;@U123&gt;"


def test_a_message_to_an_archived_channel_is_not_posted(replay) -> None:
    replay("slack/archived", ENV)
    with pytest.raises(NotPosted, match=r"Slack refused the post \(410: channel_is_archived\)"):
        SlackChannel().post("Release 2.0", ENV)


def test_check_finds_a_working_webhook_without_posting(replay) -> None:
    replay("slack/check", ENV)
    assert SlackChannel().check(ENV) == CheckResult(
        True, "the webhook answers (checked with an empty message, which Slack refuses)"
    )


def test_check_finds_a_dead_webhook(replay) -> None:
    replay("slack/check_dead", ENV)
    assert SlackChannel().check(ENV) == CheckResult(False, "the webhook is gone (404: no_service)")


def test_slack_counts_the_escaped_text() -> None:
    assert SlackChannel().count("a < b") == len("a &lt; b")
