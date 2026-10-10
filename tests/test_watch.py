"""Watching a feed: the first run posts nothing, each new item is posted once, and nothing floods.

Each test writes the feed as a file, adds to it as a feed gains items, and watches it with the
fake channel, which remembers what it was asked to post.
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import pytest

from herald import Announcement, post, preview
from herald.cli import main
from herald.feeds import Watch, feed_key, read_feed, watch
from herald.posting import _shell_word

LINK = "https://ex.co/"


def atom(path: Path, *entries: tuple[str, str], title: str = "Widget releases") -> str:
    """Write an Atom feed of ``entries``, ``(tag, title)`` newest first, to ``path``, and give its path."""
    body = "".join(
        f"<entry><id>tag:example.com,2026:{escape(tag)}</id><title>{escape(text)}</title>"
        f"<link rel='alternate' href={quoteattr(LINK + tag)}/></entry>"
        for tag, text in entries
    )
    path.write_text(
        f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>{escape(title)}</title>{body}</feed>',
        encoding="utf-8",
    )
    return str(path)


def run(ledger, source: str, channels=("fake",), **options: object) -> Watch:
    return watch(read_feed(source), feed_key(source), list(channels), ledger, **options)


def ids(report: Watch) -> list[str]:
    return [item.id.rsplit(":", 1)[-1] for item, _ in report.posted]


@pytest.fixture
def releases(tmp_path) -> Path:
    return tmp_path / "releases.atom"


def test_the_first_run_records_what_the_feed_holds_and_posts_nothing(fake, ledger, releases) -> None:
    source = atom(releases, ("v1.1.0", "v1.1.0"), ("v1.0.0", "v1.0.0"))
    report = run(ledger, source)
    assert (report.recorded, report.posted, report.ok) == ({"fake": 2}, [], True)
    assert ledger.seen(feed_key(source), "fake") == {"tag:example.com,2026:v1.1.0", "tag:example.com,2026:v1.0.0"}
    assert fake.sent == []

    again = run(ledger, source)
    assert (again.recorded, again.posted, again.ok) == ({}, [], True)
    assert fake.sent == []


def test_an_item_the_feed_gains_is_posted_once(fake, ledger, releases) -> None:
    run(ledger, atom(releases, ("v1.1.0", "v1.1.0")))
    source = atom(releases, ("v1.2.0", "Widget 1.2"), ("v1.1.0", "v1.1.0"))

    report = run(ledger, source)
    assert ids(report) == ["v1.2.0"]
    assert [outcome.status for outcome in report.posted[0][1]] == ["posted"]
    assert fake.sent == [f"Widget 1.2\n\n{LINK}v1.2.0"]

    again = run(ledger, source)
    assert (again.posted, again.ok) == ([], True)
    assert len(fake.sent) == 1


def test_new_items_are_posted_oldest_first(fake, ledger, releases) -> None:
    run(ledger, atom(releases, ("v1.1.0", "v1.1.0")))
    report = run(ledger, atom(releases, ("v1.3.0", "v1.3.0"), ("v1.2.0", "v1.2.0"), ("v1.1.0", "v1.1.0")))
    assert ids(report) == ["v1.2.0", "v1.3.0"]


def test_a_channel_added_later_has_the_feed_recorded_rather_than_flooded(fake, ledger, releases, capsys) -> None:
    run(ledger, atom(releases, ("v1.1.0", "v1.1.0")))
    source = atom(releases, ("v1.2.0", "v1.2.0"), ("v1.1.0", "v1.1.0"))
    run(ledger, source)

    widened = run(ledger, source, ("fake", "stdout"))
    assert (widened.recorded, widened.posted) == ({"stdout": 2}, [])
    assert "--- stdout ---" not in capsys.readouterr().out

    newer = run(
        ledger, atom(releases, ("v1.3.0", "v1.3.0"), ("v1.2.0", "v1.2.0"), ("v1.1.0", "v1.1.0")), ("fake", "stdout")
    )
    assert [(outcome.channel, outcome.status) for outcome in newer.posted[0][1]] == [
        ("fake", "posted"),
        ("stdout", "posted"),
    ]
    assert len(fake.sent) == 2


def test_a_post_the_platform_refused_is_tried_again_on_the_next_run(fake, ledger, releases, monkeypatch) -> None:
    run(ledger, atom(releases))
    source = atom(releases, ("v1.2.0", "v1.2.0"))
    monkeypatch.setattr(fake, "behaviour", "refuse")
    refused = run(ledger, source)
    assert (refused.ok, refused.posted[0][1][0].status) == (False, "failed")

    monkeypatch.setattr(fake, "behaviour", "post")
    retried = run(ledger, source)
    assert (retried.ok, retried.posted[0][1][0].status) == (True, "posted")
    assert run(ledger, source).posted == []
    assert len(fake.sent) == 2


def test_a_post_whose_fate_is_unknown_waits_for_a_person_every_run(fake, ledger, releases, monkeypatch) -> None:
    run(ledger, atom(releases))
    source = atom(releases, ("v1.2.0", "v1.2.0"))
    monkeypatch.setattr(fake, "behaviour", "lose")
    run(ledger, source)

    monkeypatch.setattr(fake, "behaviour", "post")
    for _ in range(2):
        report = run(ledger, source)
        assert (report.ok, report.posted[0][1][0].status) == (False, "needs-a-person")
    assert len(fake.sent) == 1, "never tried again on its own"


def test_more_new_items_than_most_posts_none_until_a_person_says(fake, ledger, releases) -> None:
    run(ledger, atom(releases, ("v1.0.0", "v1.0.0")))
    source = atom(releases, ("v1.3.0", "a"), ("v1.2.0", "b"), ("v1.1.0", "c"), ("v1.0.0", "v1.0.0"))

    held = run(ledger, source, most=2)
    assert ([item.title for item in held.held], held.posted, held.ok) == (["c", "b", "a"], [], False)
    assert fake.sent == []

    passed_over = run(ledger, source, mark_seen=True)
    assert (passed_over.recorded, passed_over.posted) == ({"fake": 4}, [])
    assert run(ledger, source, most=2).posted == []
    assert fake.sent == []


def test_the_text_may_name_the_feed_the_title_and_the_link(fake, ledger, releases) -> None:
    run(ledger, atom(releases))
    run(ledger, atom(releases, ("v2", "Two")), text="{feed}: {title} {link}")
    assert fake.sent == [f"Widget releases: Two {LINK}v2"], "the link, already in the text, is not added again"


def test_an_item_with_no_title_is_posted_as_its_link(fake, ledger, releases) -> None:
    run(ledger, atom(releases))
    run(ledger, atom(releases, ("v2", "")))
    assert fake.sent == [f"{LINK}v2"]


def test_an_item_too_long_for_a_channel_is_refused_and_the_rest_still_post(fake, ledger, releases) -> None:
    run(ledger, atom(releases))
    report = run(ledger, atom(releases, ("v3", "Short"), ("v2", "A title much too long for the fake channel's forty")))
    assert [(item.title[:5], reason.split(":")[0]) for item, reason in report.refused] == [
        ("A tit", "too long, so nothing was prepared")
    ]
    assert ids(report) == ["v3"]
    assert report.ok is False


def test_missing_credentials_are_named_from_the_first_run_on(fake, ledger, releases, monkeypatch) -> None:
    monkeypatch.delenv("FAKE_TOKEN")
    report = run(ledger, atom(releases, ("v1", "v1")))
    assert (report.recorded, report.missing, report.ok) == ({"fake": 1}, {"fake": ["FAKE_TOKEN"]}, False)


def test_an_item_posted_by_hand_is_not_posted_again(fake, ledger, releases) -> None:
    run(ledger, atom(releases))
    draft = preview(Announcement(text="By hand", id="tag:example.com,2026:v2"), ["fake"], ledger)
    post(draft.id, ledger)

    report = run(ledger, atom(releases, ("v2", "v2")))
    assert (report.posted, fake.sent) == ([], ["By hand"])


def test_an_item_listed_twice_is_posted_once(fake, ledger, releases) -> None:
    run(ledger, atom(releases))
    run(ledger, atom(releases, ("v2", "v2"), ("v2", "v2")))
    assert len(fake.sent) == 1


def test_the_ledger_keeps_what_each_feed_held_for_each_channel(ledger) -> None:
    assert ledger.seen("feed-a", "fake") is None
    ledger.record_seen("feed-a", "fake", ["1", "2"])
    ledger.record_seen("feed-a", "fake", ["3"])
    ledger.record_seen("feed-a", "stdout", [])
    ledger.record_seen("feed-b", "fake", ["9"])
    assert ledger.seen("feed-a", "fake") == {"1", "2", "3"}
    assert ledger.seen("feed-a", "stdout") == frozenset(), "watched, with nothing in it then"
    assert ledger.seen("feed-b", "stdout") is None


# The command.


def test_herald_watch_says_what_it_did(fake, ledger_dir, releases, capsys) -> None:
    common = ["--to", "fake", "--ledger-dir", str(ledger_dir)]
    source = atom(releases, ("v1.1.0", "v1.1.0"))
    assert main(["watch", source, *common]) == 0
    assert (
        capsys.readouterr().out == f"fake: recorded the 1 item {source} holds now as seen, and posted none of them.\n"
    )

    source = atom(releases, ("v1.2.0", "Widget 1.2"), ("v1.1.0", "v1.1.0"))
    assert main(["watch", source, *common]) == 0
    assert capsys.readouterr().out == "Widget 1.2:\n  fake: posted: https://fake.example/posts/1\n"

    assert main(["watch", source, *common]) == 0
    assert capsys.readouterr().out == f"Nothing new in {source}.\n"


def test_herald_watch_reports_in_json_and_exits_one_when_anything_did_not_go_out(
    fake, ledger_dir, releases, monkeypatch, capsys
) -> None:
    common = ["--to", "fake", "--ledger-dir", str(ledger_dir), "--json"]
    main(["watch", atom(releases), *common])
    capsys.readouterr()
    monkeypatch.setattr(fake, "behaviour", "refuse")

    assert main(["watch", atom(releases, ("v2", "Two")), *common]) == 1
    report = json.loads(capsys.readouterr().out)
    assert (report["source"], report["feed"], report["recorded"]) == (str(releases), "Widget releases", {})
    [posted] = report["posted"]
    assert (posted["title"], posted["link"], posted["outcomes"][0]["status"]) == ("Two", f"{LINK}v2", "failed")


def test_herald_watch_says_how_to_go_on_when_it_holds_items_back(fake, ledger_dir, releases, capsys) -> None:
    common = ["--to", "fake", "--ledger-dir", str(ledger_dir)]
    main(["watch", atom(releases), *common])
    capsys.readouterr()
    assert main(["watch", atom(releases, ("a", "a"), ("b", "b")), *common, "--most", "1"]) == 1
    assert capsys.readouterr().out == (
        "2 items are new, more than --most 1, so none was posted: if the feed changed its items' ids, they are old "
        "ones. To post them, run this again with --most 2; to pass over them, with --mark-seen.\n"
    )
    assert fake.sent == []


def test_herald_watch_refuses_a_text_before_reading_any_feed(fake, tmp_path, capsys) -> None:
    missing = tmp_path / "nowhere.atom"
    assert main(["watch", str(missing), "--to", "fake", "--text", "{summary}"]) == 1
    assert (
        capsys.readouterr().err
        == "herald: the text may name {title}, {link}, {feed}, and nothing else: it names {summary}\n"
    )


@pytest.mark.parametrize(
    ("arguments", "said"),
    [
        ([], "herald: choose at least one channel with --to\n"),
        (["--to", "fake", "--most", "0"], "herald: the most items a run posts must be at least 1\n"),
        (["--to", "nowhere"], "herald: no channel called 'nowhere'; installed: "),
    ],
)
def test_herald_watch_needs_channels_it_knows_and_a_most_of_one_or_more(
    fake, releases, capsys, arguments, said
) -> None:
    assert main(["watch", atom(releases), *arguments]) == 1
    assert capsys.readouterr().err.startswith(said)


def test_the_way_to_settle_a_post_quotes_an_id_a_shell_would_split(
    fake, ledger_dir, tmp_path, monkeypatch, capsys
) -> None:
    feed = tmp_path / "blog.rss"
    feed.write_text('<rss version="2.0"><channel><title>Blog</title></channel></rss>', encoding="utf-8")
    common = ["--to", "fake", "--ledger-dir", str(ledger_dir)]
    main(["watch", str(feed), *common])
    feed.write_text(
        '<rss version="2.0"><channel><title>Blog</title><item><title>Hi</title>'
        '<guid isPermaLink="false">https://blog.example/?p=1&amp;lang=en</guid></item></channel></rss>',
        encoding="utf-8",
    )
    monkeypatch.setattr(fake, "behaviour", "lose")
    capsys.readouterr()
    quoted = _shell_word("https://blog.example/?p=1&lang=en")
    assert quoted in ("'https://blog.example/?p=1&lang=en'", '"https://blog.example/?p=1&lang=en"')
    assert main(["watch", str(feed), *common]) == 1
    assert f"herald resolve {quoted} fake --posted" in capsys.readouterr().out
    assert main(["status", "--ledger-dir", str(ledger_dir)]) == 0
    assert f"herald resolve {quoted} fake --ledger-dir" in capsys.readouterr().out
