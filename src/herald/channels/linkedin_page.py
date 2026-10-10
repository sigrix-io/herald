"""LinkedIn page: a post as an organisation, through LinkedIn's Community Management API.

LinkedIn grants that API only after vetting the company behind the app, and on an app of its own,
so this channel reads its own app's id and secret. The person who signs in must hold one of the
page's roles that may post as it: administrator, content admin, or direct sponsored content
poster. The app needs ``http://localhost:8765/callback`` among its redirect URLs, and its sign-in
asks for ``w_organization_social`` alone.

LinkedIn gives approved apps a refresh token with the sign-in. It lasts a year from that sign-in,
and renewing with it does not extend that year. Each post renews from it an access token of its
own, so the page posts for the year with no one signing in, and ``herald check`` warns two weeks
before it lapses. An app LinkedIn gives no refresh token gets the access token alone, which lasts
60 days, as a profile's does; the channel posts with that instead when it is set.

Text is escaped and counted as on a profile; see :mod:`herald.channels.linkedin`.
"""

from __future__ import annotations

import webbrowser
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from herald import http
from herald.channels import Channel, CheckResult, NotPosted, PostResult, SignIn
from herald.channels.linkedin import (
    DEFAULT_PORT,
    DEFAULT_VERSION,
    OAUTH,
    WARN_DAYS,
    browser_sign_in,
    escape,
    exchange_code,
    publish,
)
from herald.counting import utf16_length

SCOPES = "w_organization_social"
ORGANIZATION = "HERALD_LINKEDIN_PAGE_ORGANIZATION"
CLIENT_ID = "HERALD_LINKEDIN_PAGE_CLIENT_ID"
CLIENT_SECRET = "HERALD_LINKEDIN_PAGE_CLIENT_SECRET"  # noqa: S105 - the variable's name, not a secret
REFRESH_TOKEN = "HERALD_LINKEDIN_PAGE_REFRESH_TOKEN"  # noqa: S105 - the variable's name, not a token
REFRESH_TOKEN_EXPIRES = "HERALD_LINKEDIN_PAGE_REFRESH_TOKEN_EXPIRES"  # noqa: S105 - a name
TOKEN = "HERALD_LINKEDIN_PAGE_TOKEN"  # noqa: S105 - the variable's name, not a token
TOKEN_EXPIRES = "HERALD_LINKEDIN_PAGE_TOKEN_EXPIRES"  # noqa: S105 - a name
_URN = "urn:li:organization:"


def _moment(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _later(seconds: Any) -> datetime | None:
    if isinstance(seconds, int) and seconds > 0:
        return (datetime.now(UTC) + timedelta(seconds=seconds)).replace(microsecond=0)
    return None


class LinkedInPageChannel(Channel):
    name = "linkedin-page"
    label = "LinkedIn page"
    limit = 3000
    #: Every variable it reads a credential from, the access token included, so that an error quoting
    #: one shows its name. :meth:`missing_credentials` says which of them a page needs.
    credentials = (ORGANIZATION, CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, TOKEN)

    def count(self, text: str) -> int:
        return utf16_length(escape(text))

    def missing_credentials(self, env: Mapping[str, str]) -> list[str]:
        """The page and either a refresh token with its app, or an access token alone."""
        needed = [ORGANIZATION] if self.setting(env, TOKEN) else [ORGANIZATION, CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN]
        return [name for name in needed if not self.setting(env, name)]

    def organization(self, env: Mapping[str, str]) -> str:
        """The page's URN, from its number or its URN. Raises ``ValueError`` for anything else."""
        value = self.setting(env, ORGANIZATION)
        number = value.removeprefix(_URN)
        if not number.isdigit():
            raise ValueError(
                f"{ORGANIZATION} should be the page's number, as in linkedin.com/company/NUMBER/admin, or its URN"
            )
        return f"{_URN}{number}"

    def _renew(self, env: Mapping[str, str]) -> http.Response:
        return http.post_form(
            f"{OAUTH}/accessToken",
            {
                "grant_type": "refresh_token",
                "refresh_token": self.setting(env, REFRESH_TOKEN),
                "client_id": self.setting(env, CLIENT_ID),
                "client_secret": self.setting(env, CLIENT_SECRET),
            },
        )

    def _access_token(self, env: Mapping[str, str]) -> str:
        stated = self.setting(env, TOKEN)
        if stated:
            return stated
        answer = http.before_posting("Renewing the LinkedIn page's token", lambda: self._renew(env))
        if not answer.ok:
            raise NotPosted(
                f"LinkedIn refused the refresh token ({answer.status}: {http.detail(answer)}); "
                "run herald auth linkedin-page"
            )
        token = str((answer.json() or {}).get("access_token") or "")
        if not token:
            raise NotPosted("LinkedIn's answer to the refresh token held no access token")
        return token

    def post(self, text: str, env: Mapping[str, str]) -> PostResult:
        try:
            author = self.organization(env)
        except ValueError as error:
            raise NotPosted(str(error)) from None
        token = self._access_token(env)
        return publish(author, text, token, self.setting(env, "HERALD_LINKEDIN_VERSION", DEFAULT_VERSION))

    def check(self, env: Mapping[str, str]) -> CheckResult:
        missing = self.missing_credentials(env)
        if missing:
            return CheckResult(
                False, "set " + ", ".join(missing) + "; herald auth linkedin-page gives the refresh token"
            )
        try:
            page = self.organization(env)
        except ValueError as error:
            return CheckResult(False, str(error))
        if self.setting(env, TOKEN):
            return self._lapse(page, "token", _moment(self.setting(env, TOKEN_EXPIRES)), TOKEN_EXPIRES)
        try:
            answer = self._renew(env)
        except Exception as error:
            return CheckResult(False, f"could not reach LinkedIn: {type(error).__name__}: {error}")
        if answer.status in (400, 401):
            return CheckResult(
                False,
                f"LinkedIn refused the refresh token ({http.detail(answer)}), which lasts a year from the sign-in: "
                "run herald auth linkedin-page",
            )
        if not answer.ok:
            return CheckResult(False, f"LinkedIn answered {answer.status} ({http.detail(answer)})")
        grant = answer.json() or {}
        expires = _later(grant.get("refresh_token_expires_in")) or _moment(self.setting(env, REFRESH_TOKEN_EXPIRES))
        return self._lapse(page, "refresh token", expires, REFRESH_TOKEN_EXPIRES)

    @staticmethod
    def _lapse(page: str, what: str, expires: datetime | None, setting: str) -> CheckResult:
        if expires is None:
            return CheckResult(
                True,
                f"posting as {page}; set {setting}, which herald auth linkedin-page gives, "
                f"to be warned before the {what} lapses",
            )
        days = (expires - datetime.now(UTC)).days
        if days < 0:
            return CheckResult(False, f"the {what} lapsed on {expires:%Y-%m-%d}: run herald auth linkedin-page")
        if days <= WARN_DAYS:
            return CheckResult(
                True,
                f"posting as {page}, but the {what} lapses on {expires:%Y-%m-%d}, in {days} days: "
                "run herald auth linkedin-page before then",
            )
        return CheckResult(True, f"posting as {page} until {expires:%Y-%m-%d}")

    def sign_in(
        self,
        env: Mapping[str, str],
        *,
        port: int = DEFAULT_PORT,
        open_browser: Callable[[str], object] = webbrowser.open,
        say: Callable[[str], object] = print,
        wait: float = 300.0,
    ) -> SignIn:
        """Sign in through the browser and return the refresh token, or the access token LinkedIn gave instead.

        A page administrator signs in once. With a refresh token, that lasts a year.
        """
        client, secret = self.setting(env, CLIENT_ID), self.setting(env, CLIENT_SECRET)
        if not (client and secret):
            raise ValueError(f"set {CLIENT_ID} and {CLIENT_SECRET}, from the page app's Auth tab")
        code, redirect = browser_sign_in(client, SCOPES, port=port, open_browser=open_browser, say=say, wait=wait)
        grant = exchange_code(code, redirect, client, secret)
        refresh = str(grant.get("refresh_token") or "")
        if refresh:
            settings = {REFRESH_TOKEN: refresh}
            secret_name = REFRESH_TOKEN
            lapses = _later(grant.get("refresh_token_expires_in"))
            summary = "Signed in to post as the page."
            if lapses is not None:
                settings[REFRESH_TOKEN_EXPIRES] = lapses.isoformat()
                summary += f" The refresh token lasts until {lapses:%Y-%m-%d}; sign in again before then."
        else:
            settings = {TOKEN: str(grant["access_token"])}
            secret_name = TOKEN
            lapses = _later(grant.get("expires_in"))
            summary = "Signed in to post as the page, but LinkedIn gave no refresh token, which it gives approved apps."
            if lapses is not None:
                settings[TOKEN_EXPIRES] = lapses.isoformat()
                summary += f" The token lasts until {lapses:%Y-%m-%d}; sign in again before then."
        if not self.setting(env, ORGANIZATION):
            summary += f" Set {ORGANIZATION} to the page's number, from linkedin.com/company/NUMBER/admin."
        return SignIn(settings=settings, secret=secret_name, summary=summary)


__all__ = ["LinkedInPageChannel"]
