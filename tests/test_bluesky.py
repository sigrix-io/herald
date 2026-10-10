"""Bluesky, against recorded exchanges: sign in with an app password, then post with link facets."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from herald import Announcement, CheckResult, NotPosted, http, post, preview
from herald.channels.bluesky import BlueskyChannel, link_facets

ENV = {"HERALD_BLUESKY_HANDLE": "herald-test.bsky.social", "HERALD_BLUESKY_APP_PASSWORD": "abcd-efgh-ijkl-mnop"}
TEXT = "Version 2.0 is out \N{EM DASH} read about it: https://example.com/releases/2.0"
FAMILY = "".join(map(chr, (0x1F468, 0x200D, 0x1F469, 0x200D, 0x1F467, 0x200D, 0x1F466)))


def test_a_post_links_its_link_at_its_byte_offsets(replay) -> None:
    replayed = replay("bluesky/post", ENV)

    result = BlueskyChannel().post(TEXT, ENV)

    assert result.url == "https://bsky.app/profile/herald-test.bsky.social/post/3l2s5xxv2ze2c"
    assert result.id.startswith("at://did:plc:")
    created = json.loads(replayed.sent[1].body)["record"]["createdAt"]
    assert created.endswith("Z")
    assert datetime.fromisoformat(created.replace("Z", "+00:00")).tzinfo is not None


def test_link_facets_count_bytes_not_characters() -> None:
    text = "\N{EM DASH} https://a.example/x and (https://b.example/y)."
    facets = link_facets(text)
    assert [facet["features"][0]["uri"] for facet in facets] == ["https://a.example/x", "https://b.example/y"]
    for facet, link in zip(facets, ("https://a.example/x", "https://b.example/y"), strict=True):
        index = facet["index"]
        assert text.encode("utf-8")[index["byteStart"] : index["byteEnd"]].decode("utf-8") == link
    assert link_facets("no links here") == []


def test_a_wrong_app_password_is_not_posted(replay) -> None:
    replayed = replay("bluesky/bad_password", ENV)
    with pytest.raises(NotPosted, match=r"Bluesky refused the sign-in \(401: Invalid identifier or password\)"):
        BlueskyChannel().post(TEXT, ENV)
    assert len(replayed.sent) == 1, "nothing was posted after the refusal"


def test_a_sign_in_that_gets_no_answer_is_not_posted(monkeypatch) -> None:
    def lost(request: http.Request, timeout: float) -> http.Response:
        raise TimeoutError("timed out")

    monkeypatch.setattr(http, "transport", lost)
    with pytest.raises(NotPosted, match="Signing in to Bluesky failed, before anything was posted"):
        BlueskyChannel().post(TEXT, ENV)


def test_a_record_bluesky_refuses_is_not_posted(replay) -> None:
    replay("bluesky/too_long", ENV)
    with pytest.raises(NotPosted, match="must not be longer than 300 graphemes"):
        BlueskyChannel().post(TEXT, ENV)


def test_a_bad_gateway_after_the_post_leaves_it_uncertain(replay) -> None:
    replay("bluesky/bad_gateway", ENV)
    with pytest.raises(http.Uncertain, match="Bluesky answered 502"):
        BlueskyChannel().post(TEXT, ENV)


def test_no_answer_to_the_post_is_left_for_a_person(replay) -> None:
    replay("bluesky/no_answer", ENV)
    with pytest.raises(TimeoutError) as lost:
        BlueskyChannel().post(TEXT, ENV)
    assert not isinstance(lost.value, NotPosted)


def test_check_signs_in_and_names_the_account(replay) -> None:
    """A handle given with its @ signs in as the handle without it."""
    replay("bluesky/check", ENV)
    result = BlueskyChannel().check({**ENV, "HERALD_BLUESKY_HANDLE": "@herald-test.bsky.social"})
    assert result == CheckResult(True, "signed in as @herald-test.bsky.social")


def test_check_says_why_it_cannot_sign_in(replay) -> None:
    replay("bluesky/bad_password", ENV)
    result = BlueskyChannel().check(ENV)
    assert not result.ok
    assert "Invalid identifier or password" in result.detail
    assert ENV["HERALD_BLUESKY_APP_PASSWORD"] not in result.detail


def test_graphemes_are_the_count_until_the_bytes_run_out() -> None:
    channel = BlueskyChannel()
    assert channel.count(TEXT) == len(TEXT)
    assert channel.count(FAMILY * 100) == 250, "a hundred families are a hundred graphemes but 2,500 bytes"
    assert channel.count(FAMILY * 121) > channel.limit, "3,025 bytes is over Bluesky's 3,000"


def test_herald_posts_a_bluesky_draft_end_to_end(replay, ledger) -> None:
    replay("bluesky/post", ENV)
    draft = preview(Announcement(text=TEXT, link="https://example.com/releases/2.0", id="v2.0"), ["bluesky"], ledger)

    [outcome] = post(draft.id, ledger, env=ENV)

    assert (outcome.status, outcome.url) == (
        "posted",
        "https://bsky.app/profile/herald-test.bsky.social/post/3l2s5xxv2ze2c",
    )
    assert ledger.state("v2.0", "bluesky").url == outcome.url
