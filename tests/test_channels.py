"""Channels are plugins, the built-in ones included, and two packages cannot both own a name."""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

import pytest

from herald import Channel, ChannelConflict, UnknownChannel, discover_channels, get_channel, register
from herald.channels import ENTRY_POINT_GROUP
from herald.channels.stdout import StdoutChannel
from herald.cli import main

PLUGIN = """
from herald import Channel


class Echo(Channel):
    name = "echo"
    label = "Echo"
"""
README = Path(__file__).resolve().parent.parent / "README.md"


def test_the_built_in_channel_comes_through_the_entry_point_group() -> None:
    [entry] = [entry for entry in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP) if entry.name == "stdout"]

    assert entry.value == "herald.channels.stdout:StdoutChannel"
    [found] = [found for found in discover_channels() if found.name == "stdout"]
    assert found.channel is StdoutChannel
    assert found.source == f"herald-post {importlib.metadata.version('herald-post')}"


def test_a_channel_registered_in_code_is_listed_with_its_module(capsys) -> None:
    @register
    class Local(Channel):
        name = "local"
        label = "Local"

    assert main(["channels", "--json"]) == 0
    assert '"source": "test_channels"' in capsys.readouterr().out
    assert isinstance(get_channel("LOCAL"), Local)


def test_two_packages_registering_one_name_are_refused(install_plugin, capsys) -> None:
    install_plugin("herald-echo-one", "herald_echo_one", PLUGIN, {"echo": "herald_echo_one:Echo"})
    install_plugin("herald-echo-two", "herald_echo_two", PLUGIN, {"echo": "herald_echo_two:Echo"})

    with pytest.raises(ChannelConflict, match=r"'echo' is registered by both herald-echo-"):
        discover_channels()
    assert main(["channels"]) == 1
    assert "registered by both" in capsys.readouterr().err


def test_code_cannot_take_a_name_a_package_has(install_plugin) -> None:
    install_plugin("herald-echo", "herald_echo", PLUGIN, {"echo": "herald_echo:Echo"})

    @register
    class Echo(Channel):
        name = "echo"

    with pytest.raises(ChannelConflict, match=r"herald-echo 1\.0"):
        discover_channels()


def test_an_entry_point_must_carry_its_channels_own_name(install_plugin) -> None:
    install_plugin("herald-echo", "herald_echo", PLUGIN, {"shout": "herald_echo:Echo"})

    with pytest.raises(ValueError, match=r"entry point 'shout' from herald-echo 1\.0 names a channel called 'echo'"):
        discover_channels()


def test_an_entry_point_that_is_not_a_channel_is_named(install_plugin) -> None:
    install_plugin("herald-echo", "herald_echo", "class Echo:\n    name = 'echo'\n", {"echo": "herald_echo:Echo"})

    with pytest.raises(ValueError, match="is not a herald Channel"):
        discover_channels()


def test_a_plugin_that_fails_to_import_is_named_rather_than_raised(install_plugin, capsys) -> None:
    install_plugin("herald-echo", "herald_echo", "import no_such_module_anywhere\n", {"echo": "herald_echo:Echo"})

    assert main(["channels"]) == 1
    error = capsys.readouterr().err
    assert "the 'echo' channel from herald-echo 1.0 could not be loaded" in error
    assert "ModuleNotFoundError" in error


def test_an_unknown_channel_names_the_installed_ones(capsys) -> None:
    installed = ", ".join(found.name for found in discover_channels())
    assert "stdout" in installed
    with pytest.raises(UnknownChannel, match=f"no channel called 'nowhere'; installed: {installed}$"):
        get_channel("nowhere")
    assert main(["preview", "--to", "nowhere", "--text", "Hello"]) == 1
    assert f"installed: {installed}" in capsys.readouterr().err


def test_a_channel_needs_a_lower_case_name() -> None:
    class Shouting(Channel):
        name = "Shouting"

    with pytest.raises(ValueError, match="lower-case"):
        register(Shouting)


def test_a_channel_without_post_cannot_post() -> None:
    class Mute(Channel):
        name = "mute"

    with pytest.raises(NotImplementedError, match="the mute channel cannot post"):
        Mute().post("Hello", {})


def _readme_channels() -> dict[str, list[str]]:
    """The README's Channels table: each row's cells after the channel's name, by that name."""
    section = README.read_text(encoding="utf-8").split("\n## Channels\n", 1)[1]
    rows: dict[str, list[str]] = {}
    for line in section.splitlines():
        if line.startswith("| `"):
            name, *cells = (cell.strip() for cell in line.strip("|").split("|"))
            rows[name.strip("`")] = cells
        elif rows and not line.startswith("|"):
            break
    return rows


def test_the_readme_gives_every_built_in_channel_a_row_with_its_limit_and_credentials() -> None:
    """The README restates the channels, so it is held to the entry points that register them."""
    rows = _readme_channels()
    assert "stdout" in rows, "the sweep found the README's table"
    built_in = {
        entry.name: entry.load()
        for entry in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
        if entry.value.startswith("herald.channels.")
    }
    assert sorted(rows) == sorted(built_in)
    for name, channel in built_in.items():
        limit, _counted, variables = rows[name]
        assert limit.split(" ")[0].rstrip(",") == ("none" if channel.limit is None else f"{channel.limit:,}"), name
        for credential in channel.credentials:
            assert f"`{credential}`" in variables, f"{name}'s row names {credential}"
