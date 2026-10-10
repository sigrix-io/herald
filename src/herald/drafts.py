"""Fit and preview: the exact text each channel will get, as a draft with an id.

Preparing a draft makes no network call. A post sends a draft and nothing else, so the text a
person (or an assistant's user) was shown is the text that goes out.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from herald.announcement import Announcement
from herald.channels import Channel


@dataclass(frozen=True)
class DraftItem:
    """One channel's exact text, and how long it is to that platform."""

    channel: str
    text: str
    count: int
    limit: int | None


@dataclass(frozen=True)
class Draft:
    """What a preview showed, ready to post. Its ``id`` is derived from that content."""

    id: str
    announcement_id: str
    items: tuple[DraftItem, ...]
    created_at: str

    def to_json(self) -> dict[str, object]:
        return {**asdict(self), "items": [asdict(item) for item in self.items]}

    @classmethod
    def from_json(cls, data: dict[str, object]) -> Draft:
        known = DraftItem.__dataclass_fields__
        items = tuple(
            DraftItem(**{key: value for key, value in item.items() if key in known})  # type: ignore[arg-type]
            for item in data["items"]  # type: ignore[union-attr]
        )
        return cls(
            id=str(data["id"]),
            announcement_id=str(data["announcement_id"]),
            items=items,
            created_at=str(data["created_at"]),
        )


class TooLong(Exception):
    """At least one channel's text is over its limit. Nothing is cut to fit."""

    def __init__(self, items: Sequence[DraftItem]) -> None:
        self.items = tuple(items)
        parts = [f"{item.channel} is {item.count} of {item.limit}" for item in self.items]
        super().__init__("too long, so nothing was prepared: " + "; ".join(parts))


def prepare(announcement: Announcement, channels: Sequence[Channel]) -> Draft:
    """Fit ``announcement`` to each of ``channels`` and return the draft, or raise :class:`TooLong`."""
    if not channels:
        raise ValueError("choose at least one channel")
    names = [channel.name for channel in channels]
    if len(set(names)) != len(names):
        raise ValueError(f"a channel is chosen twice: {names}")
    items = []
    for channel in channels:
        if not announcement.text_for(channel.name).strip():
            raise ValueError(f"there is no text for {channel.name}")
        text = channel.render(announcement)
        items.append(DraftItem(channel.name, text, channel.count(text), channel.limit))
    over = [item for item in items if item.limit is not None and item.count > item.limit]
    if over:
        raise TooLong(over)
    content = json.dumps(
        {"announcement": announcement.id, "items": [[item.channel, item.text] for item in items]},
        ensure_ascii=False,
        sort_keys=True,
    )
    draft_id = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    created = datetime.now(UTC).isoformat(timespec="seconds")
    return Draft(id=draft_id, announcement_id=announcement.id, items=tuple(items), created_at=created)


__all__ = ["Draft", "DraftItem", "TooLong", "prepare"]
