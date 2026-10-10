"""Herald's promises, as the issue that specified it put them, each against a fake channel.

A preview makes no network call; a post sends only a previewed draft; a second post of the same
announcement is refused; over-long text is refused with its count; an entry left in *sending* is
reported rather than retried; and a plugin registered from another package shows in
``herald channels``.
"""

from __future__ import annotations

import urllib.request

import pytest

from herald import Announcement, TooLong, UnknownDraft, post, preview
from herald.cli import main


def test_the_network_guard_refuses_a_connection(no_network: None) -> None:
    """The guard the next test leans on, seen failing: without it, no network call proves nothing."""
    with pytest.raises(AssertionError, match="a network call was made"):
        urllib.request.urlopen("http://example.com/", timeout=1)


def test_a_preview_makes_no_network_call(fake, no_network, ledger, ledger_dir, capsys) -> None:
    draft = preview(Announcement(text="Hello", link="https://example.com/a"), ["fake"], ledger)
    assert main(["preview", "--to", "fake", "--text", "Hello again", "--ledger-dir", str(ledger_dir)]) == 0

    assert "Nothing was sent." in capsys.readouterr().out
    assert fake.sent == []
    assert fake.checks == 0
    assert ledger.state(draft.announcement_id, "fake").state == "drafted"


def test_a_post_sends_only_a_previewed_draft(fake, ledger, ledger_dir, capsys) -> None:
    with pytest.raises(UnknownDraft, match="preview the announcement"):
        post("0123456789ab", ledger)
    assert main(["post", "0123456789ab", "--ledger-dir", str(ledger_dir)]) == 1
    assert "preview the announcement" in capsys.readouterr().err
    assert fake.sent == []

    draft = preview(Announcement(text="Shipped.", link="https://example.com/v1"), ["fake"], ledger)
    [outcome] = post(draft.id, ledger)

    assert outcome.status == "posted"
    assert fake.sent == [draft.items[0].text] == ["Shipped.\n\nhttps://example.com/v1"]


def test_a_second_post_of_the_same_announcement_is_refused(fake, ledger, ledger_dir, capsys) -> None:
    first = preview(Announcement(text="Version 1 is out.", id="v1"), ["fake"], ledger)
    [posted] = post(first.id, ledger)
    assert posted.status == "posted"

    [again] = post(first.id, ledger)
    reworded = preview(Announcement(text="Version 1 is out, at last.", id="v1"), ["fake"], ledger)
    assert reworded.id != first.id
    [reworded_outcome] = post(reworded.id, ledger)

    assert again.status == reworded_outcome.status == "already-posted"
    assert again.url == reworded_outcome.url == posted.url
    assert fake.sent == ["Version 1 is out."]
    assert main(["post", reworded.id, "--ledger-dir", str(ledger_dir)]) == 0, "posted earlier is what was asked"
    assert "already posted, so not again" in capsys.readouterr().out
    assert fake.sent == ["Version 1 is out."]


def test_over_long_text_is_refused_with_its_count(fake, ledger, ledger_dir, capsys) -> None:
    text = "x" * 41

    with pytest.raises(TooLong) as refused:
        preview(Announcement(text=text), ["fake"], ledger)
    assert str(refused.value) == "too long, so nothing was prepared: fake is 41 of 40"
    assert [(item.channel, item.count, item.limit) for item in refused.value.items] == [("fake", 41, 40)]

    assert main(["preview", "--to", "fake", "--text", text, "--ledger-dir", str(ledger_dir)]) == 1
    assert "fake is 41 of 40" in capsys.readouterr().err
    assert ledger.latest() == []
    assert not (ledger_dir / "drafts.jsonl").exists(), "a refused preview keeps nothing"


def test_the_link_herald_adds_counts_toward_the_limit(fake, ledger) -> None:
    """Eighteen characters fit; with the blank line and the 21-character link Herald adds, they do not."""
    preview(Announcement(text="x" * 18), ["fake"], ledger)
    with pytest.raises(TooLong, match="fake is 41 of 40"):
        preview(Announcement(text="x" * 18, link="https://example.com/x"), ["fake"], ledger)


def test_an_entry_left_in_sending_is_reported_rather_than_retried(fake, ledger, ledger_dir, capsys) -> None:
    draft = preview(Announcement(text="Big news.", id="news"), ["fake"], ledger)
    fake.behaviour = "crash"
    with pytest.raises(KeyboardInterrupt):
        post(draft.id, ledger)
    assert ledger.state("news", "fake").state == "sending"

    fake.behaviour = "post"
    [outcome] = post(draft.id, ledger)

    assert outcome.status == "needs-a-person"
    assert "herald resolve news fake" in outcome.detail
    assert fake.sent == ["Big news."], "the post that may exist was not made again"
    assert main(["status", "--ledger-dir", str(ledger_dir)]) == 0
    status = capsys.readouterr().out
    assert "sending" in status
    assert "herald resolve news fake" in status

    assert main(["resolve", "news", "fake", "--not-posted", "--ledger-dir", str(ledger_dir)]) == 0
    [retried] = post(draft.id, ledger)
    assert retried.status == "posted"
    assert fake.sent == ["Big news.", "Big news."]


def test_a_post_that_got_no_answer_waits_for_a_person(fake, ledger) -> None:
    draft = preview(Announcement(text="Big news."), ["fake"], ledger)
    fake.behaviour = "lose"

    [lost] = post(draft.id, ledger)
    fake.behaviour = "post"
    [later] = post(draft.id, ledger)

    assert lost.status == later.status == "needs-a-person"
    assert lost.detail.startswith("TimeoutError: ")
    assert ledger.state(draft.announcement_id, "fake").state == "sending"
    assert len(fake.sent) == 1


def test_a_post_left_in_sending_is_reported_before_anything_else(fake, ledger, monkeypatch) -> None:
    """Even with a credential gone, what the person must do first is check the platform."""
    draft = preview(Announcement(text="Big news."), ["fake"], ledger)
    fake.behaviour = "lose"
    post(draft.id, ledger)
    monkeypatch.delenv("FAKE_TOKEN")

    [outcome] = post(draft.id, ledger)

    assert outcome.status == "needs-a-person"


def test_a_person_can_record_a_post_they_found(fake, ledger, ledger_dir, capsys) -> None:
    draft = preview(Announcement(text="Big news.", id="news"), ["fake"], ledger)
    fake.behaviour = "lose"
    post(draft.id, ledger)

    url = "https://fake.example/posts/found"
    assert main(["resolve", "news", "fake", "--posted", "--url", url, "--ledger-dir", str(ledger_dir)]) == 0
    fake.behaviour = "post"
    [outcome] = post(draft.id, ledger)

    assert (outcome.status, outcome.url) == ("already-posted", url)
    assert len(fake.sent) == 1
    assert "Recorded fake as posted for news." in capsys.readouterr().out


def test_a_post_the_platform_refused_is_tried_again(fake, ledger) -> None:
    draft = preview(Announcement(text="Big news."), ["fake"], ledger)
    fake.behaviour = "refuse"
    [refused] = post(draft.id, ledger)
    assert refused.status == "failed"
    assert ledger.state(draft.announcement_id, "fake").state == "failed"

    fake.behaviour = "post"
    [posted] = post(draft.id, ledger)

    assert posted.status == "posted"
    assert len(fake.sent) == 2


def test_a_plugin_registered_from_another_package_shows_in_herald_channels(
    install_plugin, ledger, ledger_dir, capsys
) -> None:
    install_plugin(
        "herald-loud",
        "herald_loud",
        """
        from herald import Channel, PostResult

        HEARD = []


        class Loud(Channel):
            name = "loud"
            label = "Loud"
            limit = 100

            def post(self, text, env):
                HEARD.append(text.upper())
                return PostResult(url="https://loud.example/1")
        """,
        {"loud": "herald_loud:Loud"},
    )

    assert main(["channels"]) == 0
    listing = capsys.readouterr().out
    assert main(["preview", "--to", "loud", "--text", "Hello", "--ledger-dir", str(ledger_dir)]) == 0
    lines = capsys.readouterr().out.splitlines()
    [draft] = [line.split()[1].rstrip(",") for line in lines if line.startswith("Draft ")]
    assert main(["post", draft, "--ledger-dir", str(ledger_dir)]) == 0

    loud = next(line for line in listing.splitlines() if line.startswith("loud "))
    assert "herald-loud 1.0" in loud
    assert any(line.startswith("stdout ") for line in listing.splitlines()), "the built-in channel is listed beside it"
    import herald_loud

    assert herald_loud.HEARD == ["HELLO"]
