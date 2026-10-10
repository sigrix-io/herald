"""Slack: a message through an incoming webhook, whose URL is the credential.

Slack takes 40,000 characters. It reads &, < and > as markup, so they are escaped as Slack asks
and counted as escaped, in UTF-16 code units; Slack links a link in the text itself. Create the
webhook in a Slack app, under Incoming Webhooks.
"""

from __future__ import annotations

from collections.abc import Mapping

from herald import http
from herald.channels import Channel, CheckResult, PostResult
from herald.counting import utf16_length

#: What Slack answers when the webhook itself is gone, rather than the message being wrong.
_DEAD = (403, 404, 410)


def escape(text: str) -> str:
    """``text`` with the three characters Slack reads as markup escaped."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackChannel(Channel):
    name = "slack"
    label = "Slack"
    limit = 40000
    credentials = ("HERALD_SLACK_WEBHOOK_URL",)

    def count(self, text: str) -> int:
        return utf16_length(escape(text))

    def check(self, env: Mapping[str, str]) -> CheckResult:
        missing = self.missing_credentials(env)
        if missing:
            return CheckResult(False, "set " + ", ".join(missing))
        hook = self.setting(env, "HERALD_SLACK_WEBHOOK_URL")
        # A webhook answers nothing but a post, so the check sends an empty one: a working hook
        # refuses it for having no text, and a dead one says it is gone. Nothing is posted.
        try:
            answer = http.post_json(hook, {})
        except Exception as error:
            return CheckResult(False, f"could not reach {http.where(hook)}: {type(error).__name__}: {error}")
        if answer.status == 400:
            return CheckResult(True, "the webhook answers (checked with an empty message, which Slack refuses)")
        reason = http.detail(answer) or "no reason given"
        if answer.status in _DEAD:
            return CheckResult(False, f"the webhook is gone ({answer.status}: {reason})")
        return CheckResult(False, f"Slack answered {answer.status} ({reason})")

    def post(self, text: str, env: Mapping[str, str]) -> PostResult:
        answer = http.post_json(self.setting(env, "HERALD_SLACK_WEBHOOK_URL"), {"text": escape(text)})
        http.judge(answer, "Slack")
        return PostResult()


__all__ = ["SlackChannel", "escape"]
