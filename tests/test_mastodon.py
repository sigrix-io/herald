"""Mastodon, against recorded exchanges: a status with an Idempotency-Key, and the server's limit."""

from __future__ import annotations

import hashlib

import pytest

from herald import NotPosted, http
from herald.channels.mastodon import MastodonChannel

ENV = {"HERALD_MASTODON_SERVER": "mastodon.example", "HERALD_MASTODON_TOKEN": "mastodon-token-0123456789"}
TEXT = "Version 2.0 is out: https://example.com/releases/2.0"


def test_a_status_is_posted_with_a_key_that_makes_it_once(replay) -> None:
    replayed = replay("mastodon/post", ENV)

    result = MastodonChannel().post(TEXT, ENV)

    assert (result.url, result.id) == ("https://mastodon.example/@herald/113242167854930912", "113242167854930912")
    key = replayed.sent[0].headers["Idempotency-Key"]
    assert key == hashlib.sha256(f"https://mastodon.example\n{TEXT}".encode()).hexdigest()[:40]


def test_a_status_over_the_servers_limit_is_not_posted(replay) -> None:
    replay("mastodon/too_long", ENV)
    with pytest.raises(NotPosted, match=r"Mastodon refused the post \(422: Validation failed: Text character limit"):
        MastodonChannel().post(TEXT, ENV)


def test_a_visibility_mastodon_has_not_is_refused_before_sending(monkeypatch) -> None:
    monkeypatch.setattr(http, "transport", lambda request, timeout: pytest.fail("the status was sent"))
    with pytest.raises(NotPosted, match="HERALD_MASTODON_VISIBILITY must be one of"):
        MastodonChannel().post(TEXT, {**ENV, "HERALD_MASTODON_VISIBILITY": "everyone"})


def test_a_link_counts_23_however_long_it_is() -> None:
    long_link = "https://example.com/" + "a" * 100
    assert MastodonChannel().count(f"Out now: {long_link}") == len("Out now: ") + 23


def test_the_limit_is_500_unless_set(monkeypatch) -> None:
    monkeypatch.delenv("HERALD_MASTODON_MAX_CHARACTERS", raising=False)
    assert MastodonChannel().limit == 500
    monkeypatch.setenv("HERALD_MASTODON_MAX_CHARACTERS", "5000")
    assert MastodonChannel().limit == 5000
    assert MastodonChannel(max_characters=1000).limit == 1000, "an application's own value wins"
    for unusable in ("lots", "0", "-3"):
        monkeypatch.setenv("HERALD_MASTODON_MAX_CHARACTERS", unusable)
        assert MastodonChannel().limit == 500


def test_check_finds_the_server_and_the_preview_agreeing(replay, monkeypatch) -> None:
    monkeypatch.delenv("HERALD_MASTODON_MAX_CHARACTERS", raising=False)
    replay("mastodon/check", ENV)
    result = MastodonChannel().check(ENV)
    assert (result.ok, result.detail) == (True, "posting as @herald, 500 characters a post")


def test_check_says_a_larger_server_limit_can_be_used(replay, monkeypatch) -> None:
    monkeypatch.delenv("HERALD_MASTODON_MAX_CHARACTERS", raising=False)
    replay("mastodon/check_larger_server", ENV)
    result = MastodonChannel().check(ENV)
    assert result.ok
    assert "until HERALD_MASTODON_MAX_CHARACTERS=5000 is set" in result.detail


def test_check_fails_when_the_server_takes_less_than_the_preview_allows(replay, monkeypatch) -> None:
    monkeypatch.delenv("HERALD_MASTODON_MAX_CHARACTERS", raising=False)
    replay("mastodon/check_smaller_server", ENV)
    result = MastodonChannel().check(ENV)
    assert not result.ok
    assert result.detail.endswith("set HERALD_MASTODON_MAX_CHARACTERS=400")


def test_check_names_a_refused_token_without_it(replay) -> None:
    replay("mastodon/check_bad_token", ENV)
    result = MastodonChannel().check(ENV)
    assert not result.ok
    assert "The access token is invalid" in result.detail
    assert ENV["HERALD_MASTODON_TOKEN"] not in result.detail
