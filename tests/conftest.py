"""What every test shares: a fake channel, a ledger of its own, and no network.

Herald must be installed (``pip install -e '.[dev]'``): its own channels are found through the
entry point group, as anyone's are, and that needs the package's metadata.
"""

from __future__ import annotations

import importlib.metadata
import socket
import sys
import textwrap
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import ClassVar

import pytest

import herald.channels as channels_module
import herald.http
from exchanges import Replay
from herald import Channel, JsonLinesLedger, NotPosted, PostResult, register

#: The fake channel's credential, as tests set it. Nothing Herald prints or records may contain it.
TOKEN = "fake-token-6f1d0c9e2b"


def pytest_configure(config: pytest.Config) -> None:
    try:
        importlib.metadata.distribution("herald-post")
    except importlib.metadata.PackageNotFoundError:
        raise pytest.UsageError("install herald before testing it: pip install -e '.[dev]'") from None


class FakeChannel(Channel):
    """A channel that posts nowhere, remembers what it was asked, and fails when told to."""

    name = "fake"
    label = "Fake"
    limit = 40
    credentials = ("FAKE_TOKEN",)

    sent: ClassVar[list[str]] = []
    checks: ClassVar[int] = 0
    #: ``post``, ``refuse`` (the platform says no), ``lose`` (no answer) or ``crash`` (Herald is killed).
    behaviour: ClassVar[str] = "post"

    def check(self, env: Mapping[str, str]):
        type(self).checks += 1
        return super().check(env)

    def post(self, text: str, env: Mapping[str, str]) -> PostResult:
        type(self).sent.append(text)
        if self.behaviour == "refuse":
            raise NotPosted(f"401 Unauthorized: token {env['FAKE_TOKEN']} is not valid")
        if self.behaviour == "lose":
            raise TimeoutError(f"no answer from https://fake.example/hooks/{env['FAKE_TOKEN']}")
        if self.behaviour == "crash":
            raise KeyboardInterrupt
        return PostResult(url=f"https://fake.example/posts/{len(self.sent)}", id=str(len(self.sent)))


@pytest.fixture(autouse=True)
def _own_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep every test's ledger, and the channels registered in code, to itself."""
    monkeypatch.setenv("HERALD_LEDGER_DIR", str(tmp_path / "default-ledger"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setattr(channels_module, "_REGISTERED", {})


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> type[FakeChannel]:
    monkeypatch.setattr(FakeChannel, "sent", [])
    monkeypatch.setattr(FakeChannel, "checks", 0)
    monkeypatch.setattr(FakeChannel, "behaviour", "post")
    monkeypatch.setenv("FAKE_TOKEN", TOKEN)
    register(FakeChannel)
    return FakeChannel


@pytest.fixture
def ledger_dir(tmp_path: Path) -> Path:
    return tmp_path / "ledger"


@pytest.fixture
def ledger(ledger_dir: Path) -> JsonLinesLedger:
    return JsonLinesLedger(ledger_dir)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any attempt to resolve a name or open a connection."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a network call was made")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture
def replay(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str, Mapping[str, str]], Replay]]:
    """Answer the channels' requests from a recorded exchange file; fail on any exchange left unused."""
    started: list[Replay] = []

    def start(name: str, env: Mapping[str, str]) -> Replay:
        replayed = Replay(name, env)
        monkeypatch.setattr(herald.http, "transport", replayed)
        started.append(replayed)
        return replayed

    yield start
    for replayed in started:
        assert not replayed.pending, f"{replayed.name}: {len(replayed.pending)} exchange(s) never requested"


@pytest.fixture
def install_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Callable[..., None]:
    """Install a distribution that registers channels, as ``pip install`` of another package would."""

    site = tmp_path / "site-packages"
    site.mkdir()
    monkeypatch.syspath_prepend(str(site))

    def install(dist: str, module: str, source: str, entry_points: Mapping[str, str]) -> None:
        (site / f"{module}.py").write_text(textwrap.dedent(source), encoding="utf-8")
        info = site / f"{dist.replace('-', '_')}-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: 1.0\n", encoding="utf-8")
        lines = "".join(f"{name} = {target}\n" for name, target in entry_points.items())
        (info / "entry_points.txt").write_text(f"[herald.channels]\n{lines}", encoding="utf-8")
        request.addfinalizer(lambda: sys.modules.pop(module, None))

    return install
