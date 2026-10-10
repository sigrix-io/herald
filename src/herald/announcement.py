"""What gets announced: the text, a link, and the text one channel should get instead."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class Announcement:
    """One thing to announce.

    ``id`` is what makes the announcement post once per channel, so give it whatever already
    names the thing announced (a release tag, a post's slug). Left empty, it is derived from the
    link and the title, or from the text when there is neither, so the same announcement made
    twice gets the same id.
    """

    text: str
    link: str = ""
    title: str = ""
    per_channel: Mapping[str, str] = field(default_factory=dict)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.text.strip() and not any(text.strip() for text in self.per_channel.values()):
            raise ValueError("an announcement needs some text")
        overrides = {str(name).strip().lower(): str(text) for name, text in self.per_channel.items()}
        object.__setattr__(self, "per_channel", MappingProxyType(overrides))
        if not self.id:
            basis = f"{self.link}\n{self.title}" if (self.link or self.title) else self.text
            object.__setattr__(self, "id", "a-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16])

    def text_for(self, channel: str) -> str:
        """The text written for ``channel``, else the announcement's own."""
        return self.per_channel.get(channel, self.text)

    @classmethod
    def from_file(cls, path: str | Path, **overrides: Any) -> Announcement:
        """Read an announcement from a ``.json`` file, or take a text file's content as its text."""
        source = Path(path)
        raw = source.read_text(encoding="utf-8")
        return cls.from_text(raw, as_json=source.suffix.lower() == ".json", source=str(source), **overrides)

    @classmethod
    def from_text(cls, raw: str, *, as_json: bool, source: str = "the input", **overrides: Any) -> Announcement:
        """An announcement from ``raw``: a JSON object, or the announcement's text itself.

        A JSON object holds ``text`` and optionally ``link``, ``title``, ``id`` and ``channels``
        (a channel's name to its own text). Keyword arguments that are not empty win over it.
        """

        if as_json:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError(f"{source} must hold a JSON object")
            channels = data.get("channels") or {}
            if not isinstance(channels, dict):
                raise ValueError(f"{source}: channels must map a channel's name to its text")
            fields: dict[str, Any] = {
                "text": str(data.get("text") or ""),
                "link": str(data.get("link") or ""),
                "title": str(data.get("title") or ""),
                "id": str(data.get("id") or ""),
                "per_channel": channels,
            }
        else:
            fields = {"text": raw.strip()}
        fields.update({key: value for key, value in overrides.items() if value})
        return cls(**fields)


__all__ = ["Announcement"]
