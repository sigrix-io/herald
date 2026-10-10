"""Herald posts one announcement to every channel you choose, after showing exactly what each one will get.

The flow is three steps, and the middle one is the point:

1. **Fit** each channel's text, counted the way that platform counts, refusing it when it is too long.
2. **Preview** the exact text for every channel, which makes a draft with an id. No network call.
3. **Post** that draft, and only that draft, once per channel, recording each result in a ledger.
"""

from __future__ import annotations

from herald.announcement import Announcement
from herald.channels import (
    Channel,
    ChannelConflict,
    CheckResult,
    Installed,
    NotPosted,
    PostResult,
    SignIn,
    UnknownChannel,
    available_channels,
    discover_channels,
    get_channel,
    register,
)
from herald.drafts import Draft, DraftItem, TooLong, prepare
from herald.ledger import Entry, JsonLinesLedger, Ledger, LedgerBusy, default_ledger_dir
from herald.posting import Outcome, UnknownDraft, post, preview, resolve

__version__ = "0.1.0"

__all__ = [
    "Announcement",
    "Channel",
    "ChannelConflict",
    "CheckResult",
    "Draft",
    "DraftItem",
    "Entry",
    "Installed",
    "JsonLinesLedger",
    "Ledger",
    "LedgerBusy",
    "NotPosted",
    "Outcome",
    "PostResult",
    "SignIn",
    "TooLong",
    "UnknownChannel",
    "UnknownDraft",
    "__version__",
    "available_channels",
    "default_ledger_dir",
    "discover_channels",
    "get_channel",
    "post",
    "prepare",
    "preview",
    "register",
    "resolve",
]
