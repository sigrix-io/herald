"""Channels: one class per platform, found through the ``herald.channels`` entry point group.

A channel says how long a post may be, how its platform counts, which credentials it reads from
the environment, how to check them and how to post. The built-in channels register through the
same entry point group as anyone else's (see ``pyproject.toml``), so a channel another package
publishes is as much a part of Herald as these. An application that embeds Herald and does not
want to package a channel can :func:`register` one instead.

A channel never stores, prints or logs a credential. Its errors name a variable, never its value.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from herald.announcement import Announcement

#: The entry point group a package registers its channels under, ``name = "module:Class"``.
ENTRY_POINT_GROUP = "herald.channels"


class NotPosted(Exception):
    """The platform refused the post, so nothing was posted and it is safe to try again.

    Raise this only when the platform's own answer says so. Any other exception from
    :meth:`Channel.post`, a timeout after the request was sent included, leaves the post's
    fate unknown, and Herald then waits for a person rather than risk posting twice.
    """


class ChannelConflict(Exception):
    """Two packages registered a channel under the same name."""


class UnknownChannel(LookupError):
    """No installed channel has that name."""


@dataclass(frozen=True)
class PostResult:
    """What a platform said about a post it accepted."""

    url: str = ""
    id: str = ""


@dataclass(frozen=True)
class CheckResult:
    """Whether a channel's credentials work, in words a person can act on."""

    ok: bool
    detail: str = ""


class Channel:
    """One platform. Subclass it, set the class attributes, and implement :meth:`post`."""

    #: The name the channel is chosen by, as in ``--to bluesky``. Lower case.
    name: ClassVar[str] = ""
    #: What a person calls it.
    label: ClassVar[str] = ""
    #: The longest post the platform takes, counted by :meth:`count`; ``None`` for no limit.
    limit: ClassVar[int | None] = None
    #: The environment variables the channel reads its credentials from.
    credentials: ClassVar[tuple[str, ...]] = ()

    def count(self, text: str) -> int:
        """How long ``text`` is to this platform. Code points unless the channel says otherwise."""
        return len(text)

    def render(self, announcement: Announcement) -> str:
        """The exact text this channel posts: its own text, with the link added if it is missing."""
        text = announcement.text_for(self.name).strip()
        if announcement.link and announcement.link not in text:
            text = f"{text}\n\n{announcement.link}" if text else announcement.link
        return text

    def missing_credentials(self, env: Mapping[str, str]) -> list[str]:
        """The credential variables that are unset or empty."""
        return [name for name in self.credentials if not str(env.get(name) or "").strip()]

    def check(self, env: Mapping[str, str]) -> CheckResult:
        """Whether the credentials are there. A channel that can ask its platform should."""
        if not self.credentials:
            return CheckResult(True, "needs no credentials")
        missing = self.missing_credentials(env)
        if missing:
            return CheckResult(False, "set " + ", ".join(missing))
        return CheckResult(True, "credentials are set (not checked with the platform)")

    def post(self, text: str, env: Mapping[str, str]) -> PostResult:
        """Post ``text`` exactly as given. Raise :class:`NotPosted` only when nothing was posted."""
        raise NotImplementedError(f"the {self.name or type(self).__name__} channel cannot post")


_REGISTERED: dict[str, type[Channel]] = {}


@dataclass(frozen=True)
class Installed:
    """A channel Herald found, and where: a distribution and its version, or a module."""

    name: str
    channel: type[Channel]
    source: str


def register(channel: type[Channel]) -> type[Channel]:
    """Make ``channel`` available without packaging it. Usable as a class decorator."""
    if not channel.name or channel.name != channel.name.lower():
        raise ValueError(f"{channel.__name__}.name must be a lower-case name, got {channel.name!r}")
    existing = _REGISTERED.get(channel.name)
    if existing is not None and existing is not channel:
        raise ChannelConflict(f"{channel.name!r} is already registered by {existing.__module__}.{existing.__name__}")
    _REGISTERED[channel.name] = channel
    return channel


def discover_channels() -> list[Installed]:
    """Every channel Herald can post to, by name: the entry point group's, then those registered in code."""
    found: dict[str, Installed] = {}
    for entry in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        dist = entry.dist
        source = f"{dist.name} {dist.version}" if dist is not None else entry.value
        try:
            channel = entry.load()
        except Exception as error:
            raise ValueError(
                f"the {entry.name!r} channel from {source} could not be loaded: {type(error).__name__}: {error}"
            ) from error
        if not (isinstance(channel, type) and issubclass(channel, Channel)):
            raise ValueError(f"{entry.value} (entry point {entry.name!r}, from {source}) is not a herald Channel")
        if channel.name != entry.name:
            raise ValueError(f"entry point {entry.name!r} from {source} names a channel called {channel.name!r}")
        earlier = found.get(entry.name)
        if earlier is not None and earlier.channel is not channel:
            raise ChannelConflict(f"{entry.name!r} is registered by both {earlier.source} and {source}")
        found[entry.name] = Installed(entry.name, channel, source)
    for name, channel in _REGISTERED.items():
        earlier = found.get(name)
        if earlier is not None and earlier.channel is not channel:
            raise ChannelConflict(f"{name!r} is registered by both {earlier.source} and {channel.__module__}")
        if earlier is None:
            found[name] = Installed(name, channel, channel.__module__)
    return [found[name] for name in sorted(found)]


def available_channels() -> dict[str, type[Channel]]:
    """Every channel Herald can post to, by name."""
    return {installed.name: installed.channel for installed in discover_channels()}


def get_channel(name: str) -> Channel:
    """A ready instance of the channel called ``name``."""
    channels = available_channels()
    key = name.strip().lower()
    if key not in channels:
        known = ", ".join(channels) or "none"
        raise UnknownChannel(f"no channel called {name!r}; installed: {known}")
    return channels[key]()


__all__ = [
    "ENTRY_POINT_GROUP",
    "Channel",
    "ChannelConflict",
    "CheckResult",
    "Installed",
    "NotPosted",
    "PostResult",
    "UnknownChannel",
    "available_channels",
    "discover_channels",
    "get_channel",
    "register",
]
