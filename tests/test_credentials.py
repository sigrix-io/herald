"""Credentials come from the environment by name, and no value is ever shown or recorded."""

from __future__ import annotations

import urllib.parse

import pytest

from conftest import TOKEN
from herald import Announcement, discover_channels, post, preview
from herald.cli import main
from herald.posting import redact


def _everything_written(ledger_dir) -> str:
    return "".join(path.read_text(encoding="utf-8") for path in ledger_dir.iterdir())


def test_check_names_a_missing_credential_without_a_value(fake, monkeypatch, capsys) -> None:
    monkeypatch.delenv("FAKE_TOKEN")
    assert main(["check", "--to", "fake"]) == 1
    assert capsys.readouterr().out == "fake: not ready: set FAKE_TOKEN\n"

    monkeypatch.setenv("FAKE_TOKEN", TOKEN)
    assert main(["check", "--to", "fake"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("fake: ready")
    assert TOKEN not in output


def test_check_with_no_channel_named_checks_every_one(fake, monkeypatch, capsys) -> None:
    """Every installed channel is checked; one without its credentials is named, offline, as not ready."""
    others = [found for found in discover_channels() if found.name not in ("fake", "stdout")]
    assert len(others) >= 6, "the built-in platform channels are installed"
    for found in others:
        for name in found.channel.credentials:
            monkeypatch.delenv(name, raising=False)

    assert main(["check"]) == 1

    lines = capsys.readouterr().out.splitlines()
    assert lines[[found.name for found in discover_channels()].index("fake")] == (
        "fake: ready: credentials are set (not checked with the platform)"
    )
    assert "stdout: ready: needs no credentials" in lines
    for found in others:
        [line] = [line for line in lines if line.startswith(f"{found.name}: ")]
        assert line.startswith(f"{found.name}: not ready: set {found.channel.credentials[0]}")


def test_a_check_that_quotes_the_token_shows_its_name(fake, monkeypatch, capsys) -> None:
    def leaky(self, env):
        raise RuntimeError(f"the platform rejected {env['FAKE_TOKEN']}")

    monkeypatch.setattr(fake, "check", leaky)
    assert main(["check", "--to", "fake"]) == 1
    output = capsys.readouterr().out
    assert TOKEN not in output
    assert "the platform rejected $FAKE_TOKEN" in output


def test_a_refusal_that_quotes_the_token_is_shown_and_recorded_without_it(fake, ledger, ledger_dir, capsys) -> None:
    draft = preview(Announcement(text="Hello"), ["fake"], ledger)
    fake.behaviour = "refuse"

    [outcome] = post(draft.id, ledger)
    assert main(["post", draft.id, "--ledger-dir", str(ledger_dir)]) == 1

    assert outcome.detail == "401 Unauthorized: token $FAKE_TOKEN is not valid"
    assert ledger.state(draft.announcement_id, "fake").error == outcome.detail
    assert TOKEN not in capsys.readouterr().out
    assert TOKEN not in _everything_written(ledger_dir)
    assert len(fake.sent) == 2


def test_a_webhook_url_is_hidden_whole_and_by_its_parts(fake, ledger, monkeypatch) -> None:
    hook = "https://hooks.example/services/T0001/B0002/s3cretPart9x"
    monkeypatch.setenv("FAKE_TOKEN", hook)

    assert redact(f"POST {hook} failed", ["FAKE_TOKEN"], {"FAKE_TOKEN": hook}) == "POST $FAKE_TOKEN failed"
    for form in ("/services/T0001/B0002/s3cretPart9x", "s3cretPart9x", urllib.parse.quote(hook, safe="")):
        message = redact(f"no answer for {form}", ["FAKE_TOKEN"], {"FAKE_TOKEN": hook})
        assert "s3cret" not in message, message
    draft = preview(Announcement(text="Hello"), ["fake"], ledger)
    fake.behaviour = "lose"
    [outcome] = post(draft.id, ledger)
    assert "s3cret" not in outcome.detail
    assert "fake.example/hooks/$FAKE_TOKEN" in outcome.detail


def test_a_value_too_short_to_be_a_secret_is_left_alone() -> None:
    assert redact("chat 12345 is full", ["CHAT_ID"], {"CHAT_ID": "12345"}) == "chat 12345 is full"


def test_a_post_with_a_credential_missing_records_nothing(fake, ledger, monkeypatch) -> None:
    draft = preview(Announcement(text="Hello"), ["fake"], ledger)
    monkeypatch.setenv("FAKE_TOKEN", "  ")

    [outcome] = post(draft.id, ledger)

    assert (outcome.status, outcome.detail) == ("missing-credentials", "set FAKE_TOKEN")
    assert ledger.state(draft.announcement_id, "fake").state == "drafted"
    assert fake.sent == []


@pytest.mark.parametrize("command", [["channels"], ["check"], ["status"]])
def test_no_command_prints_a_credential(fake, ledger, command, capsys) -> None:
    draft = preview(Announcement(text="Hello"), ["fake"], ledger)
    post(draft.id, ledger)
    main([*command, "--json"])
    main(command)
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
