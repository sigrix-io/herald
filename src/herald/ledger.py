"""The ledger: the drafts previews made, and what happened to each post.

Each announcement and channel moves from *drafted* to *sending*, then to *posted* or *failed*.
A post is recorded as *sending* before the platform is asked, so a crash between the two leaves
that record behind, and an entry left in *sending* is never retried on its own: the platform may
already have the post. ``herald status`` shows it, a person checks the platform, and ``herald
resolve`` records what they found.

:class:`Ledger` is the interface. :class:`JsonLinesLedger` keeps it in two append-only files in
one directory, which suits the command line and a CI job that caches the directory. An
application with a database of its own implements the interface over that instead.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from herald.drafts import Draft

State = Literal["drafted", "sending", "posted", "failed"]

#: The states that hold a channel for an announcement. Only a post's result or a person moves
#: an entry out of them, and a preview never does.
HOLDING = frozenset({"sending", "posted"})


@dataclass(frozen=True)
class Entry:
    """What happened to one announcement on one channel. ``error`` never carries a credential."""

    announcement_id: str
    channel: str
    state: State
    draft_id: str
    at: str
    url: str = ""
    error: str = ""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def fold(entries: Iterable[Entry]) -> dict[tuple[str, str], Entry]:
    """The entry that stands for each announcement and channel, from every entry in the order written.

    The latest entry stands, except that a *drafted* one never replaces a *sending* or *posted*
    one: previewing an announcement again changes nothing about a post made or under way.
    """

    standing: dict[tuple[str, str], Entry] = {}
    for entry in entries:
        key = (entry.announcement_id, entry.channel)
        current = standing.get(key)
        if entry.state == "drafted" and current is not None and current.state in HOLDING:
            continue
        standing.pop(key, None)
        standing[key] = entry
    return standing


class LedgerBusy(Exception):
    """Another process holds the ledger, or a crash left its lock behind."""


class Ledger(Protocol):
    """Where Herald keeps drafts and results. An application may implement it over its own store."""

    def save_draft(self, draft: Draft) -> None: ...

    def load_draft(self, draft_id: str) -> Draft:
        """The draft with that id; ``KeyError`` when there is none."""
        ...

    def record(self, entry: Entry) -> None:
        """Keep ``entry`` before returning."""
        ...

    def claim(self, entry: Entry) -> Entry | None:
        """Keep the *sending* ``entry`` unless the channel is already held for that announcement.

        This is the step that makes a post happen once, so it must be atomic: of two callers
        claiming the same announcement and channel, one gets ``None`` and posts, and the other
        gets the entry that holds it. A store that cannot compare and set in one step locks.
        """
        ...

    def state(self, announcement_id: str, channel: str) -> Entry | None:
        """The entry that stands for that announcement and channel (see :func:`fold`)."""
        ...

    def latest(self, announcement_id: str | None = None) -> list[Entry]:
        """The entry that stands for each announcement and channel, in the order they last moved."""
        ...


_ENTRY_FIELDS = frozenset(Entry.__dataclass_fields__)


def _entry(record: dict[str, object]) -> Entry:
    """An entry from its JSON record, leaving out any field a later version added."""
    return Entry(**{key: value for key, value in record.items() if key in _ENTRY_FIELDS})  # type: ignore[arg-type]


def default_ledger_dir() -> Path:
    """``HERALD_LEDGER_DIR``, else ``$XDG_STATE_HOME/herald``, else ``~/.local/state/herald``."""
    explicit = os.environ.get("HERALD_LEDGER_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "herald"


class JsonLinesLedger:
    """A ledger in ``drafts.jsonl`` and ``ledger.jsonl`` under ``directory``. Append-only.

    Every write holds ``ledger.lock``, created exclusively, so two processes sharing the
    directory cannot both claim a post. A process killed while writing can leave the lock
    behind; the next write waits ``lock_timeout`` seconds for it, then says to delete it.
    """

    def __init__(self, directory: str | Path | None = None, *, lock_timeout: float = 10.0) -> None:
        self.directory = Path(directory) if directory is not None else default_ledger_dir()
        self.lock_timeout = lock_timeout
        self._drafts = self.directory / "drafts.jsonl"
        self._entries = self.directory / "ledger.jsonl"
        self._lock = self.directory / "ledger.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                handle = os.open(self._lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise LedgerBusy(
                        f"{self._lock} is held. If no herald is running, a crash left it behind: delete it."
                    ) from None
                time.sleep(0.05)
        try:
            os.write(handle, f"{os.getpid()}\n".encode())
            os.close(handle)
            yield
        finally:
            self._lock.unlink(missing_ok=True)

    def _append(self, path: Path, record: dict[str, object]) -> None:
        """Append one line. Called with the lock held, so a line without its end is a crash's."""
        line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        with path.open("ab") as handle:
            size = handle.seek(0, os.SEEK_END)
            if size:
                with path.open("rb") as reader:
                    reader.seek(size - 1)
                    if reader.read(1) != b"\n":
                        reader.seek(0)
                        handle.truncate(reader.read().rfind(b"\n") + 1)
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _read(self, path: Path) -> Iterator[dict[str, object]]:
        """Each complete line's record. A last line without its end is a write not finished."""
        if not path.exists():
            return
        content = path.read_bytes()
        complete = content[: content.rfind(b"\n") + 1]
        for number, line in enumerate(complete.decode("utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path} line {number} is not JSON ({error}); mend or remove that line") from None
            if not isinstance(record, dict):
                raise ValueError(f"{path} line {number} is not a JSON object; mend or remove that line")
            yield record

    def _standing(self) -> dict[tuple[str, str], Entry]:
        return fold(_entry(record) for record in self._read(self._entries))

    def save_draft(self, draft: Draft) -> None:
        with self._locked():
            self._append(self._drafts, draft.to_json())

    def load_draft(self, draft_id: str) -> Draft:
        wanted = draft_id.strip()
        for record in self._read(self._drafts):
            if record.get("id") == wanted:
                return Draft.from_json(record)
        raise KeyError(draft_id)

    def record(self, entry: Entry) -> None:
        with self._locked():
            self._append(self._entries, asdict(entry))

    def claim(self, entry: Entry) -> Entry | None:
        with self._locked():
            current = self._standing().get((entry.announcement_id, entry.channel))
            if current is not None and current.state in HOLDING:
                return current
            self._append(self._entries, asdict(entry))
            return None

    def state(self, announcement_id: str, channel: str) -> Entry | None:
        return self._standing().get((announcement_id, channel))

    def latest(self, announcement_id: str | None = None) -> list[Entry]:
        entries = self._standing().values()
        return [entry for entry in entries if announcement_id is None or entry.announcement_id == announcement_id]


__all__ = [
    "HOLDING",
    "Entry",
    "JsonLinesLedger",
    "Ledger",
    "LedgerBusy",
    "State",
    "default_ledger_dir",
    "fold",
    "now",
]
