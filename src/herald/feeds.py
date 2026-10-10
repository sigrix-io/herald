"""Feeds: post each new item of an RSS or Atom feed, such as a repository's releases, once.

:func:`read_feed` reads RSS 2.0, RSS 1.0 and Atom, from an https address or a file, and
:func:`watch` posts what is new in it. The first time a feed is watched into a channel, ``watch``
records the items the feed already holds and posts none of them, so turning a watch on never
floods a channel with old items. After that, an item is new until it is posted, and a post that
failed is tried again on the next run, as any post is.

Each item reaches the channels as an announcement whose id is the item's own, its Atom id or RSS
guid, so the ledger posts it once however many runs see it.
"""

from __future__ import annotations

import hashlib
import os
import string
import urllib.parse
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree
from xml.parsers import expat

from herald import http
from herald.announcement import Announcement
from herald.channels import get_channel
from herald.drafts import TooLong
from herald.ledger import Ledger
from herald.posting import Outcome, post, preview

#: What each post says unless told otherwise: the item's title. Its link is added where it is missing.
TEXT = "{title}"
#: What a text may name: the item's title and link, and the feed's title.
PLACES = ("title", "link", "feed")
#: The most items one run posts. More new items than this at once looks like a feed whose ids changed.
MOST = 5
#: The largest feed read, from an address or a file.
MAX_FEED = http.MAX_ANSWER

_ATOM = "{http://www.w3.org/2005/Atom}"
_RSS1 = "{http://purl.org/rss/1.0/}"
_RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
_ACCEPT = "application/atom+xml, application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.1"


@dataclass(frozen=True)
class Item:
    """One item of a feed: what identifies it, and what a post needs."""

    id: str
    title: str
    link: str


@dataclass(frozen=True)
class Feed:
    """A feed's title and its items, in the feed's own order, which is newest first in most feeds."""

    title: str
    items: tuple[Item, ...]


def _refuse_doctype(data: bytes) -> None:
    """Read ``data`` through once, refusing it at a DOCTYPE, before any entity it declares is read.

    Every XML attack the standard library is open to, entities that expand without end and
    entities read from elsewhere among them, needs a DOCTYPE. No feed needs one. The parser itself
    finds it, in whatever encoding the feed is written.
    """

    def refuse(*declaration: object) -> None:
        raise ValueError("the feed declares a DOCTYPE, which no feed needs and which can hide an XML attack")

    reader = expat.ParserCreate()
    reader.StartDoctypeDeclHandler = refuse
    try:
        reader.Parse(data, True)
    except expat.ExpatError as error:
        raise ValueError(f"the feed is not well-formed XML: {error}") from None


def parse_feed(data: bytes, base: str = "") -> Feed:
    """The feed ``data`` holds. ``base`` is the address it came from, for links given relative to it."""
    _refuse_doctype(data)
    root = ElementTree.fromstring(data)  # noqa: S314 - _refuse_doctype turned away what makes XML unsafe
    if root.tag == f"{_ATOM}feed":
        return Feed(_atom_text(root.find(f"{_ATOM}title")), tuple(_atom_items(root, base)))
    if root.tag == "rss":
        channel = root.find("channel")
        if channel is None:
            raise ValueError("the RSS feed has no channel")
        return Feed(_words(channel.findtext("title")), tuple(_rss_items(channel, base)))
    if root.tag == f"{_RDF}RDF":
        title = root.findtext(f"{_RSS1}channel/{_RSS1}title")
        return Feed(_words(title), tuple(_rdf_items(root, base)))
    raise ValueError(f"the feed is neither RSS nor Atom: it is a {root.tag!r} document")


def _atom_items(root: ElementTree.Element, base: str) -> Iterator[Item]:
    for entry in root.iterfind(f"{_ATOM}entry"):
        link = ""
        for candidate in entry.iterfind(f"{_ATOM}link"):
            if candidate.get("rel", "alternate") == "alternate" and candidate.get("href"):
                link = _absolute(candidate.get("href", ""), base)
                break
        item = _item(entry.findtext(f"{_ATOM}id"), _atom_text(entry.find(f"{_ATOM}title")), link)
        if item is not None:
            yield item


def _rss_items(channel: ElementTree.Element, base: str) -> Iterator[Item]:
    for element in channel.iterfind("item"):
        link = _absolute(_words(element.findtext("link")), base)
        guid = element.find("guid")
        guid_text = _words(guid.text if guid is not None else "")
        if not link and guid is not None and guid.get("isPermaLink", "true").strip().lower() != "false":
            link = _absolute(guid_text, base)
        item = _item(guid_text, _words(element.findtext("title")), link)
        if item is not None:
            yield item


def _rdf_items(root: ElementTree.Element, base: str) -> Iterator[Item]:
    for element in root.iterfind(f"{_RSS1}item"):
        link = _absolute(_words(element.findtext(f"{_RSS1}link")), base)
        item = _item(element.get(f"{_RDF}about"), _words(element.findtext(f"{_RSS1}title")), link)
        if item is not None:
            yield item


def _item(own_id: str | None, title: str, link: str) -> Item | None:
    """An item, identified by its own id, else by its link, else by its title; ``None`` with none of them."""
    identity = _words(own_id) or link
    if not identity and title:
        identity = "a-" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]
    return Item(identity, title, link) if identity else None


def _words(text: str | None) -> str:
    """``text`` with its whitespace, line breaks included, folded to single spaces."""
    return " ".join((text or "").split())


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _atom_text(element: ElementTree.Element | None) -> str:
    """An Atom text construct as plain text: html is read as HTML, xhtml as the markup it is."""
    if element is None:
        return ""
    kind = element.get("type", "text")
    if kind == "xhtml":
        return _words("".join(element.itertext()))
    if kind == "html":
        reader = _Text()
        reader.feed(element.text or "")
        reader.close()
        return _words("".join(reader.parts))
    return _words(element.text)


def _absolute(link: str, base: str) -> str:
    """``link`` made absolute against ``base``, if it is a web address; otherwise nothing."""
    if not link:
        return ""  # joined to the base, nothing would read as the feed's own address
    if base and urllib.parse.urlsplit(base).scheme in ("http", "https"):
        link = urllib.parse.urljoin(base, link)
    parts = urllib.parse.urlsplit(link)
    return link if parts.scheme in ("http", "https") and parts.netloc else ""


def _is_address(source: str) -> bool:
    return urllib.parse.urlsplit(source).scheme.lower() in ("http", "https")


def shown(source: str) -> str:
    """``source`` as a message may show it: an address without its query or user, which can hold a token."""
    if not _is_address(source):
        return source
    parts = urllib.parse.urlsplit(source)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return f"{parts.scheme}://{host}{parts.path}" + ("?…" if parts.query else "")


def feed_key(source: str) -> str:
    """What the ledger knows a feed by: what :func:`shown` shows, and a digest of anything it leaves out."""
    key = shown(source).removesuffix("?…")
    parts = urllib.parse.urlsplit(source)
    if _is_address(source) and (parts.query or parts.username or parts.password or parts.fragment):
        key += "#" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
    return key


def read_feed(source: str) -> Feed:
    """The feed at ``source``: an https address, or a file."""
    if _is_address(source):
        if urllib.parse.urlsplit(source).scheme.lower() != "https":
            raise ValueError(
                f"{shown(source)} is not https: a feed is read over https only, so that nobody on the way can "
                "change what gets posted"
            )
        try:
            answer = http.get(source, headers={"Accept": _ACCEPT})
        except http.Unsent as error:
            raise ValueError(str(error)) from None
        if answer.status != 200:
            moved = answer.headers.get("location", "")
            to = f", which says it moved to {shown(urllib.parse.urljoin(source, moved))}" if moved else ""
            raise ValueError(f"{shown(source)} answered {answer.status}{to}")
        data = answer.body
    else:
        with Path(source).open("rb") as handle:
            data = handle.read(MAX_FEED + 1)
    if len(data) >= MAX_FEED:
        raise ValueError(f"{shown(source)} holds more than {MAX_FEED} bytes, the most a feed may")
    return parse_feed(data, source if _is_address(source) else "")


def check_text(text: str) -> None:
    """Refuse a text that names anything but :data:`PLACES`, or that cannot be read."""
    try:
        fields = [(name, spec, conversion) for _, name, spec, conversion in string.Formatter().parse(text)]
    except ValueError as error:
        raise ValueError(f"the text cannot be read ({error}); write a lone brace as {{{{ or }}}}") from None
    for name, spec, conversion in fields:
        if name is not None and (name not in PLACES or spec or conversion):
            places = ", ".join("{" + place + "}" for place in PLACES)
            raise ValueError(f"the text may name {places}, and nothing else: it names {{{name}}}")


def _text_for(text: str, item: Item, feed: Feed) -> str:
    written = text.format_map({"title": item.title, "link": item.link, "feed": feed.title})
    return written if written.strip() else item.link


class FeedLedger(Ledger, Protocol):
    """A ledger that also keeps what each feed held when it was first watched into each channel."""

    def seen(self, feed: str, channel: str) -> frozenset[str] | None:
        """The items ``feed`` held when it was first watched into ``channel``; ``None`` if it never was."""
        ...

    def record_seen(self, feed: str, channel: str, items: Iterable[str]) -> None:
        """Keep ``items`` as seen for ``feed`` in ``channel``, so they are never posted there."""
        ...


@dataclass
class Watch:
    """What one run of :func:`watch` did."""

    feed: Feed
    #: For a channel the run recorded the feed for, posting nothing, how many items it recorded.
    recorded: dict[str, int] = field(default_factory=dict)
    #: For a channel whose credentials are not all set, the variables that are not.
    missing: dict[str, list[str]] = field(default_factory=dict)
    posted: list[tuple[Item, list[Outcome]]] = field(default_factory=list)
    #: Items that could not become a post, such as one too long for a channel, and why.
    refused: list[tuple[Item, str]] = field(default_factory=list)
    #: New items posted nowhere, because there were more of them than the most one run posts.
    held: list[Item] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether every new item went out, and every channel is ready for the next."""
        outcomes = [outcome for _, posted in self.posted for outcome in posted]
        return not (self.missing or self.refused or self.held) and all(outcome.ok for outcome in outcomes)

    def to_json(self) -> dict[str, object]:
        posted = [{**asdict(item), "outcomes": [asdict(each) for each in done]} for item, done in self.posted]
        return {
            "feed": self.feed.title,
            "recorded": self.recorded,
            "missing": self.missing,
            "posted": posted,
            "refused": [{**asdict(item), "reason": reason} for item, reason in self.refused],
            "held": [asdict(item) for item in self.held],
        }


def watch(
    feed: Feed,
    key: str,
    channels: Sequence[str],
    ledger: FeedLedger,
    *,
    text: str = TEXT,
    most: int = MOST,
    mark_seen: bool = False,
) -> Watch:
    """Post each item ``feed`` gained since it was first watched into each of ``channels``, oldest first.

    ``key`` is what the ledger knows the feed by (see :func:`feed_key`). A channel the feed has
    never been watched into gets its items recorded as seen, and none posted; so does every
    channel when ``mark_seen`` is set. ``text`` is what each post says, naming :data:`PLACES` in
    braces. When more than ``most`` items are new, none is posted, since a feed whose ids
    changed would otherwise post every old item again.
    """

    check_text(text)
    if most < 1:
        raise ValueError("the most items a run posts must be at least 1")
    chosen = [get_channel(name) for name in channels]
    if not chosen:
        raise ValueError("choose at least one channel")
    report = Watch(feed)
    items = list({item.id: item for item in reversed(feed.items)}.values())  # oldest first, each id once
    baselines: dict[str, frozenset[str]] = {}
    for channel in chosen:
        missing = channel.missing_credentials(os.environ)
        if missing:
            report.missing[channel.name] = missing
        seen = ledger.seen(key, channel.name)
        if seen is None or mark_seen:
            ledger.record_seen(key, channel.name, [item.id for item in items])
            report.recorded[channel.name] = len(items)
        else:
            baselines[channel.name] = seen
    posted = {(entry.announcement_id, entry.channel) for entry in ledger.latest() if entry.state == "posted"}
    pending = []
    for item in items:
        needed = [name for name, seen in baselines.items() if item.id not in seen and (item.id, name) not in posted]
        if needed:
            pending.append((item, needed))
    if len(pending) > most:
        report.held = [item for item, _ in pending]
        return report
    for item, needed in pending:
        try:
            announcement = Announcement(text=_text_for(text, item, feed), link=item.link, title=item.title, id=item.id)
            draft = preview(announcement, needed, ledger)
        except (TooLong, ValueError) as refusal:
            report.refused.append((item, str(refusal)))
            continue
        report.posted.append((item, post(draft.id, ledger)))
    return report


__all__ = [
    "MAX_FEED",
    "MOST",
    "PLACES",
    "TEXT",
    "Feed",
    "FeedLedger",
    "Item",
    "Watch",
    "check_text",
    "feed_key",
    "parse_feed",
    "read_feed",
    "shown",
    "watch",
]
