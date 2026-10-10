"""LinkedIn, against recorded exchanges: a profile post in little text, and the sign-in on localhost."""

from __future__ import annotations

import io
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from herald import CheckResult, NotPosted, SignIn, http
from herald.channels.linkedin import LinkedInChannel, escape
from herald.cli import main
from linkedin_browser import browser

ENV = {"HERALD_LINKEDIN_TOKEN": "linkedin-token-0123456789"}
APP = {"HERALD_LINKEDIN_CLIENT_ID": "86abcdefghijkl", "HERALD_LINKEDIN_CLIENT_SECRET": "linkedin-client-secret-xyz"}
TEXT = "Version 2.0 (our biggest yet) is out: https://example.com/releases/2.0 #release"


def test_a_post_escapes_little_text_and_links_to_the_share(replay) -> None:
    replay("linkedin/post", ENV)
    result = LinkedInChannel().post(TEXT, ENV)
    assert result.url == "https://www.linkedin.com/feed/update/urn:li:share:7239834567123456789/"
    assert result.id == "urn:li:share:7239834567123456789"


def test_a_stated_author_saves_asking_who_the_token_belongs_to(replay) -> None:
    replayed = replay("linkedin/post_as_stated_author", ENV)
    LinkedInChannel().post(TEXT, {**ENV, "HERALD_LINKEDIN_AUTHOR": "urn:li:person:782bbtaQ"})
    assert [request.url for request in replayed.sent] == ["https://api.linkedin.com/rest/posts"]


def test_a_lapsed_token_is_not_posted_and_says_what_to_run(replay) -> None:
    replay("linkedin/lapsed", ENV)
    with pytest.raises(NotPosted, match=r"revoked by the user\); run herald auth linkedin"):
        LinkedInChannel().post(TEXT, ENV)


def test_a_duplicate_linkedin_refuses_is_not_posted(replay) -> None:
    replay("linkedin/duplicate", ENV)
    with pytest.raises(NotPosted, match=r"LinkedIn refused the post \(422: Content is a duplicate"):
        LinkedInChannel().post(TEXT, ENV)


def test_every_little_text_markup_character_is_escaped() -> None:
    assert escape("\\|{}@[]()<>#*_~ plain") == "\\\\\\|\\{\\}\\@\\[\\]\\(\\)\\<\\>\\#\\*\\_\\~ plain"
    assert LinkedInChannel().count("(a)") == len("\\(a\\)")


def _in(days: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days, hours=1)).isoformat()


def test_check_says_until_when_the_token_lasts(replay) -> None:
    replay("linkedin/check", ENV)
    result = LinkedInChannel().check({**ENV, "HERALD_LINKEDIN_TOKEN_EXPIRES": _in(40)})
    assert result.ok
    assert result.detail.startswith("posting as Ada Example until ")


def test_check_warns_two_weeks_before_the_token_lapses(replay) -> None:
    replay("linkedin/check", ENV)
    result = LinkedInChannel().check({**ENV, "HERALD_LINKEDIN_TOKEN_EXPIRES": _in(10)})
    assert result.ok
    assert result.detail.endswith("in 10 days: run herald auth linkedin before then")


def test_check_asks_linkedin_when_the_app_is_known(replay) -> None:
    replay("linkedin/check_introspect", {**ENV, **APP})
    assert LinkedInChannel().check({**ENV, **APP}) == CheckResult(True, "posting as Ada Example until 2100-01-01")


def test_check_says_how_to_be_warned_when_it_cannot_tell(replay) -> None:
    replay("linkedin/check", ENV)
    result = LinkedInChannel().check(ENV)
    assert result.ok
    assert "set HERALD_LINKEDIN_TOKEN_EXPIRES" in result.detail


def test_check_fails_for_a_lapsed_token(replay) -> None:
    replay("linkedin/lapsed", ENV)
    assert LinkedInChannel().check(ENV) == CheckResult(
        False, "LinkedIn refused the token, which lapses after 60 days: run herald auth linkedin"
    )


def test_signing_in_returns_the_token_the_member_and_when_it_lapses(replay) -> None:
    replay("linkedin/sign_in", {**ENV, **APP})
    sent_to: list[str] = []

    result = LinkedInChannel().sign_in(
        APP, port=0, open_browser=browser(sent_to=sent_to), say=lambda text: None, wait=10
    )

    assert result.secret == "HERALD_LINKEDIN_TOKEN"
    assert result.settings["HERALD_LINKEDIN_TOKEN"] == "AQX-new-linkedin-token-0123456789"
    assert result.settings["HERALD_LINKEDIN_AUTHOR"] == "urn:li:person:782bbtaQ"
    lapses = datetime.fromisoformat(result.settings["HERALD_LINKEDIN_TOKEN_EXPIRES"])
    assert timedelta(days=59) < lapses - datetime.now(UTC) <= timedelta(days=60)
    assert result.summary.startswith("Signed in as Ada Example. The token lasts until ")
    [sign_in] = sent_to
    assert sign_in.startswith("https://www.linkedin.com/oauth/v2/authorization?")
    assert "scope=openid+profile+w_member_social" in sign_in
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A" in sign_in


def test_the_terminal_and_the_browser_get_only_the_start_page_on_this_machine(replay) -> None:
    """LinkedIn's address holds the app's id and the sign-in's state, so neither is printed or
    handed to the program that opens the browser: the start page sends the browser on to it."""
    replay("linkedin/sign_in", {**ENV, **APP})
    said: list[str] = []
    opened: list[str] = []
    sent_to: list[str] = []

    LinkedInChannel().sign_in(
        APP, port=0, open_browser=browser(opened=opened, sent_to=sent_to), say=said.append, wait=10
    )

    [start] = opened
    assert re.fullmatch(r"http://localhost:\d+/", start)
    assert said == [f"Sign in to LinkedIn in the browser. If none opens, visit {start}"]
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(sent_to[0]).query))["state"]
    for secret in (state, APP["HERALD_LINKEDIN_CLIENT_ID"]):
        assert secret not in said[0] and secret not in start


def test_a_callback_for_another_sign_in_is_ignored(monkeypatch) -> None:
    monkeypatch.setattr(http, "transport", lambda request, timeout: pytest.fail("the code was exchanged"))
    with pytest.raises(ValueError, match="came back for another request"):
        LinkedInChannel().sign_in(APP, port=0, open_browser=browser(state="forged"), say=lambda text: None, wait=10)


def test_a_cancelled_sign_in_says_why(monkeypatch) -> None:
    monkeypatch.setattr(http, "transport", lambda request, timeout: pytest.fail("the code was exchanged"))
    with pytest.raises(ValueError, match="LinkedIn did not sign you in: The member cancelled"):
        LinkedInChannel().sign_in(
            APP, port=0, open_browser=browser(error="user_cancelled_login"), say=lambda text: None, wait=10
        )


def test_a_sign_in_nobody_finishes_gives_up() -> None:
    with pytest.raises(TimeoutError, match="no sign-in came back within 0 seconds"):
        LinkedInChannel().sign_in(APP, port=0, open_browser=lambda address: None, say=lambda text: None, wait=0.2)


def test_signing_in_needs_the_app() -> None:
    with pytest.raises(ValueError, match="set HERALD_LINKEDIN_CLIENT_ID and HERALD_LINKEDIN_CLIENT_SECRET"):
        LinkedInChannel().sign_in({}, open_browser=lambda address: pytest.fail("a browser opened"))


SIGNED_IN = SignIn(
    settings={
        "HERALD_LINKEDIN_TOKEN": "AQX-new-linkedin-token-0123456789",
        "HERALD_LINKEDIN_AUTHOR": "urn:li:person:782bbtaQ",
        "HERALD_LINKEDIN_TOKEN_EXPIRES": "2026-12-09T09:00:00+00:00",
    },
    secret="HERALD_LINKEDIN_TOKEN",
    summary="Signed in as Ada Example. The token lasts until 2026-12-09; sign in again before then.",
)


@pytest.fixture
def signed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LinkedInChannel, "sign_in", lambda self, env, **options: SIGNED_IN)


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_auth_will_not_print_a_token_on_a_terminal(monkeypatch, capsys) -> None:
    monkeypatch.setattr(LinkedInChannel, "sign_in", lambda self, env, **options: pytest.fail("signed in"))
    monkeypatch.setattr(sys, "stdout", _Terminal())
    assert main(["auth", "linkedin"]) == 1
    assert "would print a credential on the terminal" in capsys.readouterr().err


def test_auth_hands_a_pipe_the_token_alone(signed_in, monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert main(["auth", "linkedin", "--no-browser"]) == 0
    assert sys.stdout.getvalue() == "AQX-new-linkedin-token-0123456789\n"
    said = capsys.readouterr().err
    assert "Also set HERALD_LINKEDIN_AUTHOR=urn:li:person:782bbtaQ" in said
    assert "AQX-new" not in said


def test_auth_writes_a_file_only_its_owner_can_read(signed_in, tmp_path, capsys) -> None:
    target = tmp_path / "linkedin.env"
    target.write_text("old\n")
    if os.name != "nt":
        target.chmod(0o644)

    assert main(["auth", "linkedin", "--output", str(target), "--no-browser"]) == 0

    assert target.read_text().splitlines() == [f"{name}={value}" for name, value in SIGNED_IN.settings.items()]
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "AQX-new" not in captured.err


def test_auth_for_a_channel_without_a_sign_in_says_what_to_set(capsys) -> None:
    assert main(["auth", "slack", "--output", "unused"]) == 1
    assert "the slack channel has no sign-in: set HERALD_SLACK_WEBHOOK_URL instead" in capsys.readouterr().err
