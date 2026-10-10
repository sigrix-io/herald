"""Telegram, against recorded exchanges: a bot posts to a channel, and its token stays out of sight."""

from __future__ import annotations

import pytest

from herald import CheckResult, NotPosted
from herald.channels.telegram import TelegramChannel
from herald.cli import main

TOKEN = "123456:telegram-bot-token-abcdef"
ENV = {"HERALD_TELEGRAM_BOT_TOKEN": TOKEN, "HERALD_TELEGRAM_CHAT_ID": "@herald_test"}
TEXT = "Version 2.0 is out: https://example.com/releases/2.0"


def test_a_message_to_a_public_channel_is_posted_and_linked(replay) -> None:
    replay("telegram/post", ENV)
    result = TelegramChannel().post(TEXT, ENV)
    assert (result.url, result.id) == ("https://t.me/herald_test/42", "42")


def test_a_bot_removed_from_the_channel_is_not_posted(replay) -> None:
    replay("telegram/not_a_member", ENV)
    with pytest.raises(NotPosted, match="Forbidden: bot is not a member of the channel chat"):
        TelegramChannel().post(TEXT, ENV)


def test_check_finds_the_bot_and_its_chat(replay) -> None:
    replay("telegram/check", ENV)
    assert TelegramChannel().check(ENV) == CheckResult(True, "@herald_test_bot posts to Herald test")


def test_check_names_a_chat_the_bot_cannot_see(replay) -> None:
    replay("telegram/check_chat_not_found", ENV)
    result = TelegramChannel().check(ENV)
    assert result == CheckResult(
        False, "@herald_test_bot cannot see chat @herald_test (400: Bad Request: chat not found)"
    )


def test_check_names_a_refused_token_without_it(replay) -> None:
    replay("telegram/check_bad_token", ENV)
    result = TelegramChannel().check(ENV)
    assert result == CheckResult(False, "Telegram refused the bot token (401: Unauthorized)")


def test_telegram_counts_utf16_code_units() -> None:
    assert TelegramChannel().count(chr(0x1F680) * 2048) == 4096


def test_a_lost_answer_quoting_the_token_reaches_no_output_and_no_ledger(
    replay, monkeypatch, ledger_dir, capsys
) -> None:
    """The error quotes the address, token and all; Herald waits for a person and shows the token to nobody."""
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    replay("telegram/no_answer", ENV)
    assert main(["preview", "--to", "telegram", "--text", TEXT, "--id", "v2.0", "--ledger-dir", str(ledger_dir)]) == 0
    lines = capsys.readouterr().out.splitlines()
    draft = next(line.split()[1].rstrip(",") for line in lines if line.startswith("Draft "))

    assert main(["post", draft, "--ledger-dir", str(ledger_dir)]) == 1
    assert main(["status", "--ledger-dir", str(ledger_dir)]) == 0

    captured = capsys.readouterr()
    assert (
        "telegram: needs a person: TimeoutError: timed out reading https://api.telegram.org/bot$HERALD_TELEGRAM_BOT_TOKEN/"
        in captured.out
    )
    assert "sending" in captured.out
    written = "".join(path.read_text(encoding="utf-8") for path in ledger_dir.iterdir())
    for seen in (captured.out, captured.err, written):
        assert TOKEN not in seen
        assert "telegram-bot-token" not in seen
