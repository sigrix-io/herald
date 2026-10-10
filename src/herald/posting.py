"""Preview and post: the two steps a person, a program or an assistant takes, in that order.

:func:`preview` fits an announcement to its channels and keeps the draft, without touching the
network. :func:`post` takes nothing but that draft's id, so what was shown is what goes out, and
posts each channel's text once per announcement. :func:`resolve` records what a person found on
a platform after a post's fate was left unknown.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from herald.announcement import Announcement
from herald.channels import Channel, NotPosted, available_channels, get_channel
from herald.drafts import Draft, DraftItem, prepare
from herald.ledger import Entry, Ledger, now

Status = Literal["posted", "already-posted", "needs-a-person", "failed", "missing-credentials"]

#: A value this short is no secret worth the name, and replacing it would garble the message.
_SHORTEST_SECRET = 6


class UnknownDraft(LookupError):
    """No draft has that id: a post sends only what a preview made."""


@dataclass(frozen=True)
class Outcome:
    """What happened on one channel when a draft was posted."""

    channel: str
    status: Status
    url: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        """Whether the channel carries the announcement, now or from an earlier post."""
        return self.status in ("posted", "already-posted")


def preview(announcement: Announcement, channels: Sequence[str | Channel], ledger: Ledger) -> Draft:
    """Fit ``announcement`` to ``channels`` and keep the draft. Makes no network call.

    Raises :class:`~herald.drafts.TooLong` when any channel's text is over its limit, and then
    keeps nothing.
    """

    chosen = [get_channel(channel) if isinstance(channel, str) else channel for channel in channels]
    draft = prepare(announcement, chosen)
    ledger.save_draft(draft)
    at = now()
    for item in draft.items:
        ledger.record(Entry(draft.announcement_id, item.channel, "drafted", draft.id, at))
    return draft


def post(
    draft_id: str,
    ledger: Ledger,
    *,
    channels: Mapping[str, Channel] | None = None,
    env: Mapping[str, str] | None = None,
) -> list[Outcome]:
    """Post the draft a preview made, each channel once per announcement.

    A channel the ledger holds as *posted* for this announcement is skipped, whichever draft
    posted it. One left in *sending* is reported for a person and not tried again. A channel
    whose credentials are missing is reported and nothing is recorded. A post the platform
    refused (:class:`~herald.channels.NotPosted`) is *failed*, and a later post may try it again;
    any other exception leaves it in *sending*, because the platform may have it.

    ``channels`` maps a name to the channel to post with, for an application that builds its
    own; by default each is found by name. ``env`` is where credentials are read from, by
    default the process environment.
    """

    try:
        draft = ledger.load_draft(draft_id)
    except KeyError:
        raise UnknownDraft(f"no draft {draft_id!r}: preview the announcement, then post the id it gives") from None
    environment = os.environ if env is None else env
    installed: Mapping[str, Channel] | None = channels
    if installed is None:
        found = available_channels()
        installed = {item.channel: found[item.channel]() for item in draft.items if item.channel in found}
    return [_post_item(draft, item, ledger, installed.get(item.channel), environment) for item in draft.items]


def _post_item(
    draft: Draft, item: DraftItem, ledger: Ledger, channel: Channel | None, env: Mapping[str, str]
) -> Outcome:
    standing = ledger.state(draft.announcement_id, item.channel)
    if standing is not None and standing.state in ("posted", "sending"):
        return _held(standing)
    if channel is None:
        return Outcome(item.channel, "failed", detail=f"the {item.channel} channel is not installed")
    missing = channel.missing_credentials(env)
    if missing:
        return Outcome(item.channel, "missing-credentials", detail="set " + ", ".join(missing))
    held = ledger.claim(Entry(draft.announcement_id, item.channel, "sending", draft.id, now()))
    if held is not None:
        return _held(held)
    try:
        result = channel.post(item.text, env)
    except NotPosted as refusal:
        detail = redact(str(refusal), channel.credentials, env) or "the platform refused the post"
        ledger.record(Entry(draft.announcement_id, item.channel, "failed", draft.id, now(), error=detail))
        return Outcome(item.channel, "failed", detail=detail)
    except Exception as unknown:
        detail = redact(f"{type(unknown).__name__}: {unknown}", channel.credentials, env)
        detail = f"{detail}. {_check_by_hand(draft.announcement_id, item.channel)}"
        return Outcome(item.channel, "needs-a-person", detail=detail)
    ledger.record(Entry(draft.announcement_id, item.channel, "posted", draft.id, now(), url=result.url))
    return Outcome(item.channel, "posted", result.url)


def _held(entry: Entry) -> Outcome:
    if entry.state == "posted":
        return Outcome(entry.channel, "already-posted", entry.url, f"posted {entry.at}")
    detail = f"a post began {entry.at} and its result was never recorded. " + _check_by_hand(
        entry.announcement_id, entry.channel
    )
    return Outcome(entry.channel, "needs-a-person", detail=detail)


def _check_by_hand(announcement_id: str, channel: str) -> str:
    return (
        f"It may have posted, so it will not be tried again: check {channel}, then run "
        f"herald resolve {announcement_id} {channel} --posted (with --url) or --not-posted"
    )


def resolve(ledger: Ledger, announcement_id: str, channel: str, *, posted: bool, url: str = "") -> Entry:
    """Record what a person found on ``channel``: that the announcement is posted there, or is not.

    It is for an entry left in *sending*, and works from any state: ``posted=True`` also records
    a post made by hand, so Herald does not make it again.
    """

    standing = ledger.state(announcement_id, channel)
    if standing is None:
        raise LookupError(f"the ledger has nothing for {announcement_id} on {channel}")
    if posted:
        entry = Entry(announcement_id, channel, "posted", standing.draft_id, now(), url=url.strip())
    else:
        entry = Entry(
            announcement_id, channel, "failed", standing.draft_id, now(), error="not posted, as a person found"
        )
    ledger.record(entry)
    return entry


def redact(text: str, names: Iterable[str], env: Mapping[str, str]) -> str:
    """``text`` with each named variable's value, in any form it travels in, replaced by its name.

    A channel's errors should never carry a credential, and this is the second line behind that:
    an HTTP library's message can quote the URL it failed on, and for a webhook the URL is the
    credential. So a URL's path, and the last segment of that path, are replaced too.
    """

    for name in names:
        value = str(env.get(name) or "").strip()
        if len(value) < _SHORTEST_SECRET:
            continue
        forms = {value, urllib.parse.quote(value, safe=""), urllib.parse.quote(value), urllib.parse.quote_plus(value)}
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme and parsed.netloc:
            forms.update({parsed.path, parsed.path.rstrip("/").rsplit("/", 1)[-1], parsed.query})
        for form in sorted((form for form in forms if len(form) >= _SHORTEST_SECRET), key=len, reverse=True):
            text = text.replace(form, f"${name}")
    return text


__all__ = ["Outcome", "Status", "UnknownDraft", "post", "preview", "redact", "resolve"]
