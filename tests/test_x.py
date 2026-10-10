"""X, against recorded exchanges: a post signed with OAuth 1.0a, counted as X counts, and a check that costs nothing."""

from __future__ import annotations

import time
import unicodedata
import urllib.parse

import pytest

import x_cases
from herald import CheckResult, NotPosted, http
from herald.channels import x_data
from herald.channels.x import PRICE, XChannel, links, signed_header, weighted_length
from herald.http import Uncertain

ENV = {
    "HERALD_X_API_KEY": "x-api-key-0123456789",
    "HERALD_X_API_SECRET": "x-api-secret-0123456789abcdef",
    "HERALD_X_ACCESS_TOKEN": "1234567890-x-access-token-0123456789",
    "HERALD_X_ACCESS_TOKEN_SECRET": "x-access-token-secret-0123456789",
}
TEXT = "Version 2.0 is out: https://example.com/releases/2.0"


def test_the_signature_is_the_one_x_documents() -> None:
    """The worked example in X's OAuth 1.0a documentation, "Creating a signature", from when it was
    Twitter's: every input is the document's, and so is the signature it arrives at."""
    header = signed_header(
        "POST",
        "https://api.twitter.com/1.1/statuses/update.json?include_entities=true",
        consumer_key="xvz1evFS4wEEPTGEFPHBog",
        consumer_secret="kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        token="370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        token_secret="LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
        form=[("status", "Hello Ladies + Gentlemen, a signed OAuth request!")],
        nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
        timestamp="1318622958",
    )
    assert header.startswith("OAuth ")
    assert 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in header
    assert 'oauth_signature_method="HMAC-SHA1"' in header


def _oauth_params(header: str) -> dict[str, str]:
    assert header.startswith("OAuth ")
    pairs = (part.strip().split("=", 1) for part in header.removeprefix("OAuth ").split(","))
    return {urllib.parse.unquote(name): urllib.parse.unquote(value.strip('"')) for name, value in pairs}


def test_a_post_is_signed_with_the_accounts_keys_and_links_to_the_post(replay) -> None:
    replayed = replay("x/post", ENV)

    result = XChannel().post(TEXT, ENV)

    assert result.url == "https://x.com/i/web/status/1445880548472328192"
    assert result.id == "1445880548472328192"
    [sent] = replayed.sent
    header = sent.headers["Authorization"]
    params = _oauth_params(header)
    assert params["oauth_consumer_key"] == ENV["HERALD_X_API_KEY"]
    assert params["oauth_token"] == ENV["HERALD_X_ACCESS_TOKEN"]
    # The signature covers this method and address with these secrets: signed again with the same
    # nonce and time, it comes out the same.
    assert header == signed_header(
        "POST",
        "https://api.x.com/2/tweets",
        consumer_key=ENV["HERALD_X_API_KEY"],
        consumer_secret=ENV["HERALD_X_API_SECRET"],
        token=ENV["HERALD_X_ACCESS_TOKEN"],
        token_secret=ENV["HERALD_X_ACCESS_TOKEN_SECRET"],
        nonce=params["oauth_nonce"],
        timestamp=params["oauth_timestamp"],
    )
    for secret in (ENV["HERALD_X_API_SECRET"], ENV["HERALD_X_ACCESS_TOKEN_SECRET"]):
        assert secret not in header


def test_each_post_is_signed_afresh() -> None:
    first, second = (
        _oauth_params(signed_header("POST", "https://api.x.com/2/tweets", **keys))["oauth_nonce"]
        for keys in [dict(consumer_key="k", consumer_secret="s", token="t", token_secret="u")] * 2
    )
    assert first != second


def test_a_duplicate_x_refuses_is_not_posted(replay) -> None:
    replay("x/duplicate", ENV)
    with pytest.raises(
        NotPosted, match=r"X refused the post \(403: You are not allowed to create a Tweet with duplicate content\.\)"
    ):
        XChannel().post(TEXT, ENV)


def test_an_outage_after_the_request_left_may_have_posted(replay) -> None:
    replay("x/unavailable", ENV)
    with pytest.raises(Uncertain, match="X answered 503: Service Unavailable, so the post may exist"):
        XChannel().post(TEXT, ENV)


def test_check_says_what_a_post_costs_and_asks_x_nothing(monkeypatch) -> None:
    monkeypatch.setattr(http, "transport", lambda request, timeout: pytest.fail("X was asked, and billed"))
    result = XChannel().check(ENV)
    assert result.ok
    assert "not checked with X, which bills every call to its API" in result.detail
    assert result.detail.endswith(f"posting is paid: {PRICE}, from prepaid credits")
    assert PRICE == "$0.015 a post, or $0.20 with a link"


def test_check_names_the_missing_keys_and_the_price() -> None:
    assert XChannel().check({}) == CheckResult(
        False,
        "set HERALD_X_API_KEY, HERALD_X_API_SECRET, HERALD_X_ACCESS_TOKEN, HERALD_X_ACCESS_TOKEN_SECRET; "
        f"posting to X is paid: {PRICE}, from prepaid credits",
    )


# How X counts, as twitter-text 3 counts: code points 0-4351, 8192-8205, 8208-8223 and 8242-8247
# weigh 1 and all others 2, an emoji weighs 2, a link 23 wherever twitter-text finds one, and text
# is NFC-normalised first. Every row was checked against twitter-text-parser 3.0.0, and the first
# two are the examples on X's "Counting characters" page.
COUNTS = [
    ("Hello, world! \U0001f44b", 16),
    ("caf\u00e9", 4),
    ("cafe\u0301", 4),  # the same word with its accent apart: X counts after NFC
    ("Version 2.0 is out: https://example.com/releases/2.0", 20 + 23),
    ("https://example.com/" + "a" * 200, 23),
    ("see https://example.com/notes.", 4 + 23 + 1),  # the full stop ends the sentence, not the link
    ("see example.com today", 4 + 23 + 6),  # a link without a scheme
    ("see sigrix.io/hub/updates/herald today", 4 + 23 + 6),
    ("mail team@example.com", 21),  # an address is no link
    ("\u8a73\u7d30\u306fexample.com\u3078", 6 + 23 + 2),  # a link in Japanese, no space around it
    ("Docs:https://example.com/x", 5 + 23),  # a link straight after a colon
    ("\u8a73\u7d30\u306fhttps://example.com/x", 6 + 23),  # and straight after a letter of another script
    ("xhttps://example.com", 20),  # but not after a letter of ASCII
    ("example.com:8080", 23 + 5),  # without a scheme, a port joins the link only with a path
    ("example.com:8080/x", 23),
    ("b\u00fccher.de", 23),  # a name in Latin letters
    ("\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444", 9),  # one in Cyrillic, a link only with a scheme
    ("https://\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444", 23),
    ("Node.js", 7),  # js is no top-level domain
    ("README.md", 23),  # but md is Moldova's
    ("https://t.co/" + "a" * 41, 13 + 41),  # a short link of X's, longer than X makes them, is not one
    ("\u65e5\u672c\u8a9e", 6),
    ("\ud55c\uad6d\uc5b4", 6),
    ("\u041f\u0440\u0438\u0432\u0435\u0442", 6),
    ("\u2014 \u201c \u2032", 5),  # a dash, a curly quote and a prime: punctuation weighs 1
    ("\u00a9 2026", 2 + 5),  # the copyright sign weighs 1 as a code point and 2 as the emoji it is
    ("\u00ae", 2),
    ("\U0001f44d\U0001f3fd", 2),  # a thumb with a skin tone
    ("\u2764\ufe0f", 2),  # a heart that U+FE0F draws as an emoji
    ("\u2600\ufe0f", 2),
    ("\u2b50\ufe0f", 4),  # a star, already an emoji, and a U+FE0F that X weighs alone
    ("\U0001f1ef\U0001f1f5", 2),  # a flag
    ("\U0001f1e6\U0001f1e6", 4),  # two regional indicators that make no flag
    ("1\ufe0f\u20e3", 2),  # a keycap
    ("line one\r\nline two", 8 + 1 + 8),  # a Windows line end is one newline to X
    ("x" * 280, 280),
    ("\u4e00" * 140, 280),
]


@pytest.mark.parametrize(("text", "weight"), COUNTS)
def test_text_weighs_what_x_says(text: str, weight: int) -> None:
    assert weighted_length(text) == weight
    assert XChannel().count(text) == weight


def _as_x_reads(text: str) -> str:
    return unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")


@pytest.mark.parametrize(("text", "found", "weight"), x_cases.CASES)
def test_text_weighs_what_twitter_text_weighs_it(text: str, found: dict[int, int], weight: int) -> None:
    """Hundreds of texts recorded with twitter-text itself: the links it finds, then the weight."""
    assert links(_as_x_reads(text)) == found
    assert weighted_length(text) == weight


# An emoji joined from others by U+200D, or spelled with tags, is one emoji to X but weighs its
# parts here: more than X's 2, so never less.
HIGH = [
    ("\U0001f469\u200d\U0001f4bb", 2 + 1 + 2),  # a woman technologist
    ("\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466", 4 * 2 + 3),  # a family
    ("\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f", 7 * 2),  # England's flag
]


@pytest.mark.parametrize(("text", "weight"), HIGH)
def test_an_emoji_joined_from_others_weighs_its_parts(text: str, weight: int) -> None:
    assert weighted_length(text) == weight


@pytest.mark.parametrize(("text", "found", "on_x"), x_cases.HIGH)
def test_what_it_weighs_differently_it_weighs_high(text: str, found: dict[int, int], on_x: int) -> None:
    assert links(_as_x_reads(text)) == found
    assert weighted_length(text) > on_x


def test_text_made_to_be_slow_is_counted_quickly() -> None:
    """twitter-text's own pattern takes half a minute over the first of these, backtracking through
    every way to read its labels. Read a label at a time, they take moments."""
    started = time.perf_counter()
    for text in ("a." * 2000, "\u65e5\u672c." * 2000, "a." * 1000 + "\u00e9_x.com", "https://a." * 800):
        weighted_length(text)
    assert time.perf_counter() - started < 2


def test_the_limit_is_280_by_x_s_count() -> None:
    channel = XChannel()
    assert channel.limit == 280
    assert channel.count("\u4e00" * 140) == 280
    assert channel.count("\u4e00" * 140 + ".") == 281


def test_the_domains_are_twitter_text_s_and_come_back_whole_from_their_ascii_form() -> None:
    generic, country = x_data.GENERIC_DOMAINS.split(), x_data.COUNTRY_DOMAINS.split()
    assert (len(generic), len(country), len(x_data.FLAGS.split())) == (1259, 315, 258)
    assert generic == sorted(set(generic)) and country == sorted(set(country))
    other_scripts = [name for name in generic + country if name.startswith("xn--")]
    assert len(other_scripts) == 95 + 60
    for name in other_scripts:
        assert name.encode("ascii").decode("idna").encode("idna").decode("ascii") == name
    assert weighted_length("https://\u4f8b\u3048.\u4e2d\u56fd") == 23  # .xn--fiqs8s, China's
