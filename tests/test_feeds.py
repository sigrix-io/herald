"""Reading feeds: RSS 2.0, RSS 1.0 and Atom, from an https address or a file, and nothing unsafe.

The feeds under ``tests/feeds/`` are written to the shapes the formats come in. The GitHub one
follows a repository's ``releases.atom``, and CI reads a live one to check that shape still holds.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

import herald.http
from herald.feeds import MAX_FEED, Item, check_text, feed_key, parse_feed, read_feed, shown
from herald.http import Request, Response, Unsent

FEEDS = Path(__file__).resolve().parent / "feeds"


def feed_of(name: str, base: str = "") -> list[Item]:
    return list(parse_feed((FEEDS / name).read_bytes(), base).items)


def test_a_repositorys_releases_are_read_newest_first() -> None:
    feed = parse_feed((FEEDS / "releases-two.atom").read_bytes())
    assert feed.title == "Release notes from widget"
    assert list(feed.items) == [
        Item(
            "tag:github.com,2008:Repository/123456789/v1.2.0",
            "Widget 1.2: a faster importer",
            "https://github.com/octo/widget/releases/tag/v1.2.0",
        ),
        Item(
            "tag:github.com,2008:Repository/123456789/v1.1.0",
            "v1.1.0",
            "https://github.com/octo/widget/releases/tag/v1.1.0",
        ),
    ]


def test_rss_items_are_known_by_their_guid_and_need_some_way_to_be_known() -> None:
    assert feed_of("blog.rss", "https://blog.example/feed/") == [
        # A guid that is not a permalink names the item, and the link is the link.
        Item(
            "https://blog.example/?p=123&lang=en",
            "Widgets, now <faster>",
            "https://blog.example/2026/10/faster-widgets/",
        ),
        # A guid that is a permalink, as guids are unless they say otherwise, is the link too.
        Item(
            "https://blog.example/2026/09/across-lines/",
            "A title that runs across lines",
            "https://blog.example/2026/09/across-lines/",
        ),
        # The item with nothing to know it by is left out; one with only a title is known by it.
        Item("a-e7518f87d3f33bee", "A title and nothing else", ""),
        # A link that is not to the web is no link.
        Item("widget-zip", "A link that is not the web", ""),
    ]
    assert parse_feed((FEEDS / "blog.rss").read_bytes()).title == "Widget & Co. blog"


def test_rss_1_items_stand_beside_the_channel() -> None:
    feed = parse_feed((FEEDS / "news.rdf").read_bytes())
    assert feed.title == "News, the RDF way"
    assert [(item.id, item.title, item.link) for item in feed.items] == [
        ("https://news.example/2", "Second", "https://news.example/2"),
        ("https://news.example/1", "First", "https://news.example/1"),
    ]


def test_atom_titles_lose_their_markup_and_links_are_read_with_care() -> None:
    feed = parse_feed((FEEDS / "marked-up.atom").read_bytes(), "https://widget.example/feed.atom")
    assert feed.title == "Widget & Co."
    assert [(item.title, item.link) for item in feed.items] == [
        ("Bold news in two lines", "https://widget.example/news/bold"),  # relative, and not the enclosure
        ("An xhtml title", "https://widget.example/news/xhtml"),  # the alternate, not the related
        ("A link that is not the web", ""),
    ]


def test_without_an_address_to_read_it_against_a_relative_link_is_no_link() -> None:
    assert feed_of("marked-up.atom")[0].link == ""


BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE feed [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
  <!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">
  <!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;">
  <!ENTITY f "&e;&e;&e;&e;&e;&e;&e;&e;&e;&e;">
  <!ENTITY g "&f;&f;&f;&f;&f;&f;&f;&f;&f;&f;">
]>
<feed xmlns="http://www.w3.org/2005/Atom"><title>&g;</title></feed>
"""


@pytest.mark.parametrize(
    "data",
    [
        BILLION_LAUGHS,
        BILLION_LAUGHS.decode()
        .replace('<?xml version="1.0"?>', '<?xml version="1.0" encoding="UTF-16"?>')
        .encode("utf-16"),
        b'<?xml version="1.0"?><!DOCTYPE rss SYSTEM "https://evil.example/rss.dtd"><rss><channel/></rss>',
    ],
    ids=["entities", "entities-in-utf-16", "a-dtd-from-elsewhere"],
)
def test_a_feed_with_a_doctype_is_refused_before_anything_in_it_is_read(data: bytes) -> None:
    with pytest.raises(ValueError, match="the feed declares a DOCTYPE"):
        parse_feed(data)


@pytest.mark.parametrize(
    ("data", "said"),
    [
        (b"<feed", "the feed is not well-formed XML"),
        (b"<html><body>Not found</body></html>", "the feed is neither RSS nor Atom: it is a 'html' document"),
        (b'<rss version="2.0"/>', "the RSS feed has no channel"),
    ],
)
def test_what_is_not_a_feed_says_so(data: bytes, said: str) -> None:
    with pytest.raises(ValueError, match=said):
        parse_feed(data)


@pytest.fixture
def answer(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[Request]]:
    """Answer the next request with ``status`` and ``body``; give back the requests made."""
    asked: list[Request] = []

    def respond(status: int = 200, body: bytes = b"", headers: dict[str, str] | None = None) -> list[Request]:
        def transport(request: Request, timeout: float) -> Response:
            asked.append(request)
            return Response(status, headers or {}, body)

        monkeypatch.setattr(herald.http, "transport", transport)
        return asked

    return respond


def test_a_feed_is_read_from_its_address(answer) -> None:
    asked = answer(body=(FEEDS / "releases-one.atom").read_bytes())
    feed = read_feed("https://github.com/octo/widget/releases.atom")
    assert [item.title for item in feed.items] == ["v1.1.0"]
    [request] = asked
    assert (request.method, request.url) == ("GET", "https://github.com/octo/widget/releases.atom")
    assert request.headers["Accept"].startswith("application/atom+xml, application/rss+xml")


def test_a_feed_that_moved_says_where_without_following(answer) -> None:
    answer(301, headers={"location": "https://github.com/octo/gadget/releases.atom?token=s3cret"})
    with pytest.raises(ValueError) as refused:
        read_feed("https://github.com/octo/widget/releases.atom")
    assert str(refused.value) == (
        "https://github.com/octo/widget/releases.atom answered 301, which says it moved to "
        "https://github.com/octo/gadget/releases.atom?…"
    )


def test_a_feed_too_large_to_read_is_refused(answer, tmp_path) -> None:
    answer(body=b" " * MAX_FEED)
    with pytest.raises(ValueError, match=f"holds more than {MAX_FEED} bytes"):
        read_feed("https://blog.example/feed/")
    large = tmp_path / "large.xml"
    large.write_bytes(b" " * (MAX_FEED + 10))
    with pytest.raises(ValueError, match=f"holds more than {MAX_FEED} bytes"):
        read_feed(str(large))


def test_a_feed_that_cannot_be_reached_says_so(monkeypatch) -> None:
    def unreachable(request: Request, timeout: float) -> Response:
        raise Unsent("could not reach https://blog.example: Name or service not known")

    monkeypatch.setattr(herald.http, "transport", unreachable)
    with pytest.raises(ValueError, match=re.escape("could not reach https://blog.example")):
        read_feed("https://blog.example/feed/")


def test_a_feed_over_plain_http_is_refused_without_asking_for_it(no_network) -> None:
    with pytest.raises(ValueError, match="is not https: a feed is read over https only"):
        read_feed("http://blog.example/feed/")


def test_a_feed_is_read_from_a_file() -> None:
    assert [item.title for item in read_feed(str(FEEDS / "news.rdf")).items] == ["Second", "First"]
    with pytest.raises(FileNotFoundError):
        read_feed(str(FEEDS / "nowhere.xml"))


def test_an_address_is_shown_without_what_could_hold_a_token() -> None:
    private = "https://octo:pa55word@github.com/octo/widget/releases.atom?token=s3cret#latest"
    assert shown(private) == "https://github.com/octo/widget/releases.atom?…"
    assert "s3cret" not in feed_key(private) and "pa55word" not in feed_key(private)
    assert feed_key(private).startswith("https://github.com/octo/widget/releases.atom#")
    assert feed_key(private) != feed_key(private.replace("s3cret", "other")), "two private feeds stay two"
    assert feed_key("https://github.com/octo/widget/releases.atom") == "https://github.com/octo/widget/releases.atom"
    assert feed_key("feeds/news.rdf") == shown("feeds/news.rdf") == "feeds/news.rdf"


@pytest.mark.parametrize("text", ["{title}", "{feed}: {title} {link}", "New: {title} {{draft}}", "No places at all"])
def test_a_text_may_name_the_title_the_link_and_the_feed(text: str) -> None:
    check_text(text)


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("{summary}", "it names {summary}"),
        ("{title.upper}", "it names {title.upper}"),
        ("{title!r}", "it names {title}"),
        ("{title:>40}", "it names {title}"),
        ("{0}", "it names {0}"),
        ("{title", "the text cannot be read"),
        ("title}", "the text cannot be read"),
    ],
)
def test_a_text_naming_anything_else_is_refused(text: str, said: str) -> None:
    with pytest.raises(ValueError, match=said.replace("{", r"\{").replace("}", r"\}")):
        check_text(text)
