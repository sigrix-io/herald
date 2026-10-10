"""A LinkedIn page, against recorded exchanges: a post as the organisation, renewed from a refresh token."""

from __future__ import annotations

import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from herald import CheckResult, NotPosted, http
from herald.channels.linkedin import LinkedInChannel
from herald.channels.linkedin_page import LinkedInPageChannel
from herald.posting import redact
from linkedin_browser import browser

APP = {
    "HERALD_LINKEDIN_PAGE_CLIENT_ID": "78pagefghijklm",
    "HERALD_LINKEDIN_PAGE_CLIENT_SECRET": "linkedin-page-client-secret-xyz",
}
ENV = {
    **APP,
    "HERALD_LINKEDIN_PAGE_ORGANIZATION": "2414183",
    "HERALD_LINKEDIN_PAGE_REFRESH_TOKEN": "AQW-page-refresh-token-0123456789",
}
TOKEN_ENV = {
    "HERALD_LINKEDIN_PAGE_ORGANIZATION": "urn:li:organization:2414183",
    "HERALD_LINKEDIN_PAGE_TOKEN": "AQV-page-token-from-a-sign-in-0123",
}
TEXT = "Version 2.0 is out: https://example.com/releases/2.0"


def test_a_post_renews_its_token_and_posts_as_the_page(replay) -> None:
    replayed = replay("linkedin_page/post", ENV)

    result = LinkedInPageChannel().post(TEXT, ENV)

    assert result.url == "https://www.linkedin.com/feed/update/urn:li:share:7239834567123456790/"
    assert result.id == "urn:li:share:7239834567123456790"
    assert [request.url for request in replayed.sent] == [
        "https://www.linkedin.com/oauth/v2/accessToken",
        "https://api.linkedin.com/rest/posts",
    ]


def test_an_access_token_posts_without_renewing(replay) -> None:
    replayed = replay("linkedin_page/post_with_token", TOKEN_ENV)
    LinkedInPageChannel().post(TEXT, TOKEN_ENV)
    assert [request.url for request in replayed.sent] == ["https://api.linkedin.com/rest/posts"]


def test_a_refused_refresh_token_is_not_posted_and_says_what_to_run(replay) -> None:
    replayed = replay("linkedin_page/refresh_refused", ENV)
    with pytest.raises(
        NotPosted,
        match=r"LinkedIn refused the refresh token \(400: The provided authorization grant or refresh token is "
        r"invalid, expired or revoked\); run herald auth linkedin-page",
    ):
        LinkedInPageChannel().post(TEXT, ENV)
    assert len(replayed.sent) == 1, "nothing was sent to the Posts API"


def test_a_renewal_that_never_arrives_is_not_posted(monkeypatch) -> None:
    def unreachable(request, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(http, "transport", unreachable)
    with pytest.raises(NotPosted, match="Renewing the LinkedIn page's token failed, before anything was posted"):
        LinkedInPageChannel().post(TEXT, ENV)


@pytest.mark.parametrize("stated", ["2414183", "urn:li:organization:2414183", " 2414183 "])
def test_the_page_is_named_by_its_number_or_its_urn(stated: str) -> None:
    assert LinkedInPageChannel().organization({"HERALD_LINKEDIN_PAGE_ORGANIZATION": stated}) == (
        "urn:li:organization:2414183"
    )


def test_a_page_named_otherwise_is_not_posted_and_nothing_is_sent(monkeypatch) -> None:
    monkeypatch.setattr(http, "transport", lambda request, timeout: pytest.fail("a request was sent"))
    with pytest.raises(NotPosted, match=r"should be the page's number, as in linkedin\.com/company/NUMBER/admin"):
        LinkedInPageChannel().post(TEXT, {**ENV, "HERALD_LINKEDIN_PAGE_ORGANIZATION": "sigrix"})


def test_either_a_refresh_token_with_its_app_or_an_access_token_will_do() -> None:
    channel = LinkedInPageChannel()
    assert channel.missing_credentials(ENV) == []
    assert channel.missing_credentials(TOKEN_ENV) == []
    assert channel.missing_credentials({"HERALD_LINKEDIN_PAGE_TOKEN": "AQV-x"}) == ["HERALD_LINKEDIN_PAGE_ORGANIZATION"]
    assert channel.missing_credentials({"HERALD_LINKEDIN_PAGE_ORGANIZATION": "2414183"}) == [
        "HERALD_LINKEDIN_PAGE_CLIENT_ID",
        "HERALD_LINKEDIN_PAGE_CLIENT_SECRET",
        "HERALD_LINKEDIN_PAGE_REFRESH_TOKEN",
    ]


def test_an_error_quoting_the_access_token_shows_its_name() -> None:
    """The access token is not needed beside a refresh token, but it is a credential all the same."""
    token = TOKEN_ENV["HERALD_LINKEDIN_PAGE_TOKEN"]
    assert redact(f"LinkedIn would not take {token}", LinkedInPageChannel.credentials, TOKEN_ENV) == (
        "LinkedIn would not take $HERALD_LINKEDIN_PAGE_TOKEN"
    )


def test_text_is_escaped_and_counted_as_on_a_profile() -> None:
    text = "Version 2.0 (our biggest yet) is out #release"
    assert LinkedInPageChannel().count(text) == LinkedInChannel().count(text)
    assert LinkedInPageChannel().limit == 3000


def test_check_says_until_when_the_refresh_token_lasts(replay) -> None:
    replay("linkedin_page/check", ENV)
    result = LinkedInPageChannel().check(ENV)
    assert result.ok
    assert result.detail.startswith("posting as urn:li:organization:2414183 until ")


def test_check_warns_two_weeks_before_the_refresh_token_lapses(replay) -> None:
    replay("linkedin_page/check_lapsing", ENV)
    result = LinkedInPageChannel().check(ENV)
    assert result.ok
    assert result.detail.endswith("in 10 days: run herald auth linkedin-page before then")
    assert "the refresh token lapses on" in result.detail


def test_check_fails_for_a_refused_refresh_token(replay) -> None:
    replay("linkedin_page/refresh_refused", ENV)
    assert LinkedInPageChannel().check(ENV) == CheckResult(
        False,
        "LinkedIn refused the refresh token (The provided authorization grant or refresh token is invalid, expired "
        "or revoked), which lasts a year from the sign-in: run herald auth linkedin-page",
    )


def test_check_with_an_access_token_reads_its_lapse_and_asks_nothing(monkeypatch) -> None:
    monkeypatch.setattr(http, "transport", lambda request, timeout: pytest.fail("LinkedIn was asked"))
    lapses = (datetime.now(UTC) + timedelta(days=40, hours=1)).isoformat()
    result = LinkedInPageChannel().check({**TOKEN_ENV, "HERALD_LINKEDIN_PAGE_TOKEN_EXPIRES": lapses})
    assert result.ok
    assert result.detail.startswith("posting as urn:li:organization:2414183 until ")


def test_check_names_what_is_missing() -> None:
    assert LinkedInPageChannel().check({}) == CheckResult(
        False,
        "set HERALD_LINKEDIN_PAGE_ORGANIZATION, HERALD_LINKEDIN_PAGE_CLIENT_ID, HERALD_LINKEDIN_PAGE_CLIENT_SECRET, "
        "HERALD_LINKEDIN_PAGE_REFRESH_TOKEN; herald auth linkedin-page gives the refresh token",
    )


def test_signing_in_returns_the_refresh_token_and_when_it_lapses(replay) -> None:
    replay("linkedin_page/sign_in", APP)
    sent_to: list[str] = []

    result = LinkedInPageChannel().sign_in(
        APP, port=0, open_browser=browser(sent_to=sent_to), say=lambda text: None, wait=10
    )

    assert result.secret == "HERALD_LINKEDIN_PAGE_REFRESH_TOKEN"
    assert result.settings["HERALD_LINKEDIN_PAGE_REFRESH_TOKEN"] == "AQW-new-page-refresh-token-0123456789"
    assert "HERALD_LINKEDIN_PAGE_TOKEN" not in result.settings, "only one credential comes back"
    lapses = datetime.fromisoformat(result.settings["HERALD_LINKEDIN_PAGE_REFRESH_TOKEN_EXPIRES"])
    assert timedelta(days=364) < lapses - datetime.now(UTC) <= timedelta(days=365)
    assert result.summary.startswith("Signed in to post as the page. The refresh token lasts until ")
    assert result.summary.endswith(
        "Set HERALD_LINKEDIN_PAGE_ORGANIZATION to the page's number, from linkedin.com/company/NUMBER/admin."
    )
    [sign_in] = sent_to
    assert dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(sign_in).query))["scope"] == "w_organization_social"


def test_an_app_given_no_refresh_token_signs_in_with_the_access_token(replay) -> None:
    replay("linkedin_page/sign_in_without_refresh", APP)
    env = {**APP, "HERALD_LINKEDIN_PAGE_ORGANIZATION": "2414183"}

    result = LinkedInPageChannel().sign_in(env, port=0, open_browser=browser(), say=lambda text: None, wait=10)

    assert result.secret == "HERALD_LINKEDIN_PAGE_TOKEN"
    assert result.settings["HERALD_LINKEDIN_PAGE_TOKEN"] == "AQV-new-page-access-token-0123456789"
    lapses = datetime.fromisoformat(result.settings["HERALD_LINKEDIN_PAGE_TOKEN_EXPIRES"])
    assert timedelta(days=59) < lapses - datetime.now(UTC) <= timedelta(days=60)
    assert "LinkedIn gave no refresh token, which it gives approved apps" in result.summary
    assert "HERALD_LINKEDIN_PAGE_ORGANIZATION" not in result.summary


def test_signing_in_needs_the_page_app() -> None:
    with pytest.raises(ValueError, match="set HERALD_LINKEDIN_PAGE_CLIENT_ID and HERALD_LINKEDIN_PAGE_CLIENT_SECRET"):
        LinkedInPageChannel().sign_in({}, open_browser=lambda address: pytest.fail("a browser opened"))
