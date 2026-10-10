"""Telegram: a bot posts to a chat or channel it belongs to.

Telegram takes 4,096 characters, counted in UTF-16 code units. The text goes as written, with no
markup to escape, and Telegram links what looks like a link. Create the bot with @BotFather, add
it to the channel as an admin that can post, and give the channel's @username or numeric id as
the chat. The token is in every request's path, so nothing here names a path.
``HERALD_TELEGRAM_API`` names a self-hosted Bot API server instead of Telegram's.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from herald import http
from herald.channels import Channel, CheckResult, PostResult
from herald.counting import utf16_length

DEFAULT_API = "https://api.telegram.org"


def _result(answer: http.Response) -> Any:
    data = answer.json()
    return data.get("result") if isinstance(data, dict) and data.get("ok") is True else None


class TelegramChannel(Channel):
    name = "telegram"
    label = "Telegram"
    limit = 4096
    credentials = ("HERALD_TELEGRAM_BOT_TOKEN", "HERALD_TELEGRAM_CHAT_ID")

    def count(self, text: str) -> int:
        return utf16_length(text)

    def _method(self, env: Mapping[str, str], method: str) -> str:
        api = self.setting(env, "HERALD_TELEGRAM_API", DEFAULT_API).rstrip("/")
        return f"{api}/bot{self.setting(env, 'HERALD_TELEGRAM_BOT_TOKEN')}/{method}"

    def check(self, env: Mapping[str, str]) -> CheckResult:
        missing = self.missing_credentials(env)
        if missing:
            return CheckResult(False, "set " + ", ".join(missing))
        chat = self.setting(env, "HERALD_TELEGRAM_CHAT_ID")
        try:
            me = http.post_json(self._method(env, "getMe"), {})
            seen = http.post_json(self._method(env, "getChat"), {"chat_id": chat}) if _result(me) else None
        except Exception as error:
            return CheckResult(False, f"could not reach Telegram: {type(error).__name__}: {error}")
        bot = _result(me)
        if not isinstance(bot, dict):
            return CheckResult(False, f"Telegram refused the bot token ({me.status}: {http.detail(me)})")
        found = _result(seen) if seen is not None else None
        if not isinstance(found, dict):
            reason = f"{seen.status}: {http.detail(seen)}" if seen is not None else "no answer"
            return CheckResult(False, f"@{bot.get('username')} cannot see chat {chat} ({reason})")
        return CheckResult(
            True, f"@{bot.get('username')} posts to {found.get('title') or found.get('username') or chat}"
        )

    def post(self, text: str, env: Mapping[str, str]) -> PostResult:
        answer = http.post_json(
            self._method(env, "sendMessage"), {"chat_id": self.setting(env, "HERALD_TELEGRAM_CHAT_ID"), "text": text}
        )
        http.judge(answer, "Telegram")
        message = _result(answer)
        if not isinstance(message, dict):
            raise http.Uncertain(f"Telegram answered {answer.status} without its usual result, so the post may exist")
        username = (message.get("chat") or {}).get("username")
        key = message.get("message_id")
        return PostResult(url=f"https://t.me/{username}/{key}" if username and key else "", id=str(key or ""))


__all__ = ["TelegramChannel"]
