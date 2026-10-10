"""The ``herald`` command's words and exit statuses, beyond the flow ``test_flow.py`` walks."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from herald import __version__
from herald.cli import main


def test_the_version_is_the_packages(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["--version"])
    assert stopped.value.code == 0
    assert capsys.readouterr().out == f"herald {__version__}\n"


def test_python_dash_m_runs_the_same_command() -> None:
    finished = subprocess.run(
        [sys.executable, "-m", "herald", "channels", "--json"], capture_output=True, text=True, check=True
    )
    assert "stdout" in [channel["name"] for channel in json.loads(finished.stdout)]


def test_a_command_used_wrongly_exits_two(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["resolve", "v1", "fake"])
    assert stopped.value.code == 2
    assert "one of the arguments --posted --not-posted is required" in capsys.readouterr().err


def test_a_preview_needs_a_channel_and_some_text(fake, capsys) -> None:
    assert main(["preview", "--text", "Hello"]) == 1
    assert main(["preview", "--to", "fake"]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "herald: choose at least one channel with --to",
        "herald: give the announcement's text with --text, or a file to read it from",
    ]


def test_a_missing_file_is_named(fake, tmp_path, capsys) -> None:
    missing = tmp_path / "nowhere.json"
    assert main(["preview", str(missing), "--to", "fake"]) == 1
    assert capsys.readouterr().err == f"herald: No such file or directory: {missing}\n"


def test_channels_may_be_repeated_or_comma_separated(fake, ledger_dir, capsys) -> None:
    arguments = ["preview", "--to", "fake,stdout", "--to", "FAKE", "--text", "Hello", "--ledger-dir", str(ledger_dir)]
    assert main([*arguments, "--json"]) == 0
    assert [item["channel"] for item in json.loads(capsys.readouterr().out)["draft"]["items"]] == ["fake", "stdout"]


def test_a_preview_says_which_channels_already_carry_the_announcement(fake, ledger_dir, capsys) -> None:
    common = ["--text", "Hello", "--id", "v1", "--ledger-dir", str(ledger_dir)]
    main(["preview", "--to", "fake", *common, "--json"])
    draft = json.loads(capsys.readouterr().out)["draft"]["id"]
    main(["post", draft, "--ledger-dir", str(ledger_dir)])
    capsys.readouterr()

    assert main(["preview", "--to", "fake,stdout", *common]) == 0
    output = capsys.readouterr().out
    assert "(Already posted " in output
    assert ": https://fake.example/posts/1. Posting this draft skips fake.)" in output
    assert output.rstrip().endswith(f"--ledger-dir {ledger_dir}")


def test_a_post_where_any_channel_was_not_posted_exits_one(fake, ledger_dir, monkeypatch, capsys) -> None:
    main(["preview", "--to", "fake,stdout", "--text", "Hello", "--ledger-dir", str(ledger_dir), "--json"])
    draft = json.loads(capsys.readouterr().out)["draft"]["id"]
    monkeypatch.delenv("FAKE_TOKEN")

    assert main(["post", draft, "--ledger-dir", str(ledger_dir), "--json"]) == 1
    outcomes = json.loads(capsys.readouterr().out.split("--- end ---\n")[-1])
    assert [(outcome["channel"], outcome["status"]) for outcome in outcomes] == [
        ("fake", "missing-credentials"),
        ("stdout", "posted"),
    ]


def test_status_with_nothing_recorded_says_where_it_looked(ledger_dir, capsys) -> None:
    assert main(["status", "--ledger-dir", str(ledger_dir)]) == 0
    assert capsys.readouterr().out == f"Nothing is recorded in {ledger_dir}.\n"
    assert main(["status", "v9", "--ledger-dir", str(ledger_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_status_shows_one_announcement_when_asked(fake, ledger_dir, capsys) -> None:
    for announcement in ("v1", "v2"):
        main(["preview", "--to", "fake", "--text", "Hello", "--id", announcement, "--ledger-dir", str(ledger_dir)])
    capsys.readouterr()

    assert main(["status", "v2", "--ledger-dir", str(ledger_dir), "--json"]) == 0
    assert [entry["announcement_id"] for entry in json.loads(capsys.readouterr().out)] == ["v2"]


def test_resolve_needs_something_to_resolve(ledger_dir, capsys) -> None:
    assert main(["resolve", "v1", "fake", "--not-posted", "--ledger-dir", str(ledger_dir)]) == 1
    assert main(["resolve", "v1", "fake", "--not-posted", "--url", "https://x", "--ledger-dir", str(ledger_dir)]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "herald: the ledger has nothing for v1 on fake",
        "herald: --url goes with --posted",
    ]
