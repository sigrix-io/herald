"""A channel that prints the post, for trying Herald and for tests. It needs no credentials."""

from __future__ import annotations

import sys
from collections.abc import Mapping

from herald.channels import Channel, PostResult


class StdoutChannel(Channel):
    name = "stdout"
    label = "Standard output"
    limit = None

    def post(self, text: str, env: Mapping[str, str]) -> PostResult:
        sys.stdout.write(f"--- stdout ---\n{text}\n--- end ---\n")
        sys.stdout.flush()
        return PostResult()


__all__ = ["StdoutChannel"]
