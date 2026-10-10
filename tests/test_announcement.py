"""An announcement: its text, its link, each channel's own text, and the id that makes it post once."""

from __future__ import annotations

import io
import json

import pytest

from herald import Announcement
from herald.cli import main


def test_the_id_comes_from_the_link_and_the_title() -> None:
    first = Announcement(text="Version 2 is out.", link="https://example.com/2", title="Version 2")
    reworded = Announcement(text="Version 2 is out, at last.", link="https://example.com/2", title="Version 2")
    other = Announcement(text="Version 2 is out.", link="https://example.com/3", title="Version 2")

    assert first.id == reworded.id != other.id
    assert first.id.startswith("a-")
    assert len(first.id) == 18


def test_the_id_comes_from_the_text_when_there_is_no_link_or_title() -> None:
    assert Announcement(text="Hello").id == Announcement(text="Hello").id != Announcement(text="Hello!").id


def test_an_id_given_is_kept() -> None:
    assert Announcement(text="Hello", link="https://example.com", id="v2.0").id == "v2.0"


def test_each_channel_may_have_its_own_text() -> None:
    announcement = Announcement(text="Shared.", per_channel={"Bluesky": "Short."})
    assert announcement.text_for("bluesky") == "Short."
    assert announcement.text_for("mastodon") == "Shared."
    with pytest.raises(TypeError):
        announcement.per_channel["mastodon"] = "Changed."  # type: ignore[index]


def test_an_announcement_needs_some_text() -> None:
    with pytest.raises(ValueError, match="needs some text"):
        Announcement(text="  ")


def test_a_channel_with_no_text_is_refused_rather_than_sent_a_bare_link(fake, ledger) -> None:
    from herald import preview

    announcement = Announcement(text="", link="https://example.com", per_channel={"stdout": "Hello"})
    with pytest.raises(ValueError, match="there is no text for fake"):
        preview(announcement, ["fake"], ledger)


def test_a_json_file_holds_every_field(tmp_path) -> None:
    path = tmp_path / "release.json"
    path.write_text(
        json.dumps(
            {
                "text": "Out now.",
                "link": "https://example.com/2",
                "title": "Two",
                "id": "v2",
                "channels": {"fake": "Hi"},
            }
        ),
        encoding="utf-8",
    )

    announcement = Announcement.from_file(path, link="https://example.com/override")

    assert (announcement.text, announcement.title, announcement.id) == ("Out now.", "Two", "v2")
    assert announcement.link == "https://example.com/override", "what the command line says wins"
    assert announcement.text_for("fake") == "Hi"


def test_a_text_file_is_the_text(tmp_path) -> None:
    path = tmp_path / "release.txt"
    path.write_text("\n  Out now.  \n", encoding="utf-8")
    assert Announcement.from_file(path).text == "Out now."


@pytest.mark.parametrize(
    ("content", "message"),
    [("[1, 2]", "must hold a JSON object"), ('{"text": "Hi", "channels": ["fake"]}', "channels must map")],
)
def test_a_json_file_of_the_wrong_shape_is_refused(tmp_path, content: str, message: str) -> None:
    path = tmp_path / "release.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        Announcement.from_file(path)


def test_standard_input_reads_json_or_text(fake, ledger_dir, monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO('{"text": "From a pipe.", "id": "piped"}'))
    assert main(["preview", "-", "--to", "fake", "--ledger-dir", str(ledger_dir)]) == 0
    assert "for announcement piped." in capsys.readouterr().out

    monkeypatch.setattr("sys.stdin", io.StringIO("Plain words.\n"))
    assert main(["preview", "-", "--to", "fake", "--ledger-dir", str(ledger_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["draft"]["items"][0]["text"] == "Plain words."
