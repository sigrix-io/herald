"""The ``herald`` command: ``channels``, ``preview``, ``post``, ``check``, ``status``, ``resolve`` and ``auth``.

``preview`` never touches the network, and ``post`` takes only a draft id that ``preview``
printed. Exit status: 0 when the command did what it was asked, 1 when it did not (a channel not
posted, text too long, a check that failed), 2 for a command used wrongly.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path

from herald import __version__
from herald.announcement import Announcement
from herald.channels import ChannelConflict, CheckResult, UnknownChannel, discover_channels, get_channel
from herald.drafts import TooLong
from herald.ledger import HOLDING, JsonLinesLedger, LedgerBusy
from herald.posting import Outcome, UnknownDraft, post, preview, redact, resolve

_OUTCOME_WORDS = {
    "posted": "posted",
    "already-posted": "already posted, so not again",
    "needs-a-person": "needs a person",
    "failed": "not posted",
    "missing-credentials": "not posted",
}


def _names(values: Sequence[str] | None) -> list[str]:
    """Channel names from repeated or comma-separated ``--to`` values, in order, once each."""
    names: list[str] = []
    for value in values or ():
        for name in value.split(","):
            key = name.strip().lower()
            if key and key not in names:
                names.append(key)
    return names


def _print_json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _print_table(rows: Sequence[Sequence[str]]) -> None:
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]) - 1)]
    for row in rows:
        cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=False)]
        print("  ".join([*cells, row[-1]]).rstrip())


def _ledger(args: argparse.Namespace) -> JsonLinesLedger:
    return JsonLinesLedger(args.ledger_dir)


def _shell_word(text: str) -> str:
    """``text`` quoted for the shell a person here would paste it into."""
    return subprocess.list2cmdline([text]) if os.name == "nt" else shlex.quote(text)


def _ledger_option(args: argparse.Namespace) -> str:
    """The ``--ledger-dir`` a follow-up command needs, if this one was given one."""
    return f" --ledger-dir {_shell_word(str(args.ledger_dir))}" if args.ledger_dir else ""


def _channels(args: argparse.Namespace) -> int:
    installed = discover_channels()
    if args.json:
        _print_json(
            [
                {
                    "name": found.name,
                    "label": found.channel.label,
                    "limit": found.channel().limit,
                    "credentials": list(found.channel.credentials),
                    "source": found.source,
                }
                for found in installed
            ]
        )
        return 0
    if not installed:
        print("No channels are installed.")
        return 0
    rows = [("NAME", "LABEL", "LIMIT", "CREDENTIALS", "FROM")]
    for found in installed:
        limit = found.channel().limit
        credentials = ", ".join(found.channel.credentials) or "none"
        shown_limit = "none" if limit is None else str(limit)
        rows.append((found.name, found.channel.label, shown_limit, credentials, found.source))
    _print_table(rows)
    return 0


def _announcement(args: argparse.Namespace) -> Announcement:
    overrides = {"text": args.text or "", "link": args.link or "", "title": args.title or "", "id": args.id or ""}
    if args.file == "-":
        raw = sys.stdin.read()
        return Announcement.from_text(raw, as_json=raw.lstrip().startswith("{"), source="standard input", **overrides)
    if args.file:
        return Announcement.from_file(args.file, **overrides)
    if not args.text:
        raise ValueError("give the announcement's text with --text, or a file to read it from")
    return Announcement(text=args.text, link=args.link or "", title=args.title or "", id=args.id or "")


def _preview(args: argparse.Namespace) -> int:
    names = _names(args.to)
    if not names:
        raise ValueError("choose at least one channel with --to")
    ledger = _ledger(args)
    draft = preview(_announcement(args), names, ledger)
    held = {
        item.channel: entry
        for item in draft.items
        if (entry := ledger.state(draft.announcement_id, item.channel)) is not None and entry.state in HOLDING
    }
    if args.json:
        _print_json({"draft": draft.to_json(), "held": {name: asdict(entry) for name, entry in held.items()}})
        return 0
    print(f"Draft {draft.id}, for announcement {draft.announcement_id}. Nothing was sent.")
    for item in draft.items:
        size = f"{item.count} of {item.limit}" if item.limit is not None else f"{item.count}, no limit"
        print(f"\n--- {item.channel}: {size} ---\n{item.text}")
        entry = held.get(item.channel)
        if entry is not None and entry.state == "posted":
            where = f": {entry.url}" if entry.url else ""
            print(f"(Already posted {entry.at}{where}. Posting this draft skips {item.channel}.)")
        elif entry is not None:
            print(f"(A post to {item.channel} began {entry.at} and never recorded its result: see herald status.)")
    print(f"\nTo post exactly this: herald post {draft.id}{_ledger_option(args)}")
    return 0


def _describe(outcome: Outcome) -> str:
    words = _OUTCOME_WORDS[outcome.status]
    details = [part for part in (outcome.detail, outcome.url) if part]
    return f"{outcome.channel}: {words}" + (f": {'; '.join(details)}" if details else "")


def _post(args: argparse.Namespace) -> int:
    outcomes = post(args.draft_id, _ledger(args))
    if args.json:
        _print_json([asdict(outcome) for outcome in outcomes])
    else:
        for outcome in outcomes:
            print(_describe(outcome))
    return 0 if all(outcome.ok for outcome in outcomes) else 1


def _check(args: argparse.Namespace) -> int:
    names = _names(args.to) or [found.name for found in discover_channels()]
    results: list[tuple[str, CheckResult]] = []
    for name in names:
        channel = get_channel(name)
        try:
            result = channel.check(os.environ)
        except Exception as error:
            result = CheckResult(False, f"{type(error).__name__}: {error}")
        results.append((name, CheckResult(result.ok, redact(result.detail, channel.credentials, os.environ))))
    if args.json:
        _print_json([{"channel": name, **asdict(result)} for name, result in results])
    else:
        for name, result in results:
            print(f"{name}: {'ready' if result.ok else 'not ready'}" + (f": {result.detail}" if result.detail else ""))
    return 0 if all(result.ok for _, result in results) else 1


def _status(args: argparse.Namespace) -> int:
    ledger = _ledger(args)
    entries = ledger.latest(args.announcement_id)
    if args.json:
        _print_json([asdict(entry) for entry in entries])
        return 0
    if not entries:
        about = f" for {args.announcement_id}" if args.announcement_id else ""
        print(f"Nothing is recorded in {ledger.directory}{about}.")
        return 0
    rows = [("ANNOUNCEMENT", "CHANNEL", "STATE", "AT", "LINK OR NOTE")]
    for entry in entries:
        if entry.state == "sending":
            note = (
                f"its result was never recorded: check {entry.channel}, then run "
                f"herald resolve {entry.announcement_id} {entry.channel}{_ledger_option(args)} --posted or --not-posted"
            )
        elif entry.state == "drafted":
            note = f"draft {entry.draft_id}, not posted"
        else:
            note = entry.url or entry.error
        rows.append((entry.announcement_id, entry.channel, entry.state, entry.at, note))
    _print_table(rows)
    return 0


def _resolve(args: argparse.Namespace) -> int:
    if args.url and not args.posted:
        raise ValueError("--url goes with --posted")
    entry = resolve(_ledger(args), args.announcement_id, args.channel.strip().lower(), posted=args.posted, url=args.url)
    print(f"Recorded {entry.channel} as {entry.state} for {entry.announcement_id}.")
    return 0


def _write_settings(path: Path, settings: Mapping[str, str]) -> None:
    """Write ``NAME=value`` lines to ``path``, readable by its owner alone."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(path, 0o600)  # a file that already existed keeps its old mode through O_CREAT
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.writelines(f"{name}={value}\n" for name, value in settings.items())


def _auth(args: argparse.Namespace) -> int:
    channel = get_channel(args.channel)
    sign_in = getattr(channel, "sign_in", None)
    if not callable(sign_in):
        needed = ", ".join(channel.credentials) or "nothing"
        raise ValueError(f"the {channel.name} channel has no sign-in: set {needed} instead")
    if args.output is None and sys.stdout.isatty():
        raise ValueError(
            f"that would print a credential on the terminal: pipe it into your secret store, as in "
            f"herald auth {channel.name} | gh secret set NAME, or pass --output FILE"
        )
    result = sign_in(
        os.environ,
        port=args.port,
        open_browser=(lambda address: None) if args.no_browser else webbrowser.open,
        say=lambda text: print(text, file=sys.stderr),
    )
    if args.output is not None:
        _write_settings(args.output, result.settings)
        print(f"{result.summary} Saved {', '.join(result.settings)} to {args.output}.", file=sys.stderr)
        return 0
    print(result.settings[result.secret])
    others = [f"{name}={value}" for name, value in result.settings.items() if name != result.secret]
    print(result.summary + (f" Also set {', '.join(others)}." if others else ""), file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ledger_option = argparse.ArgumentParser(add_help=False)
    ledger_option.add_argument(
        "--ledger-dir",
        type=Path,
        metavar="DIR",
        help="where drafts and results are kept (default: $HERALD_LEDGER_DIR, else $XDG_STATE_HOME/herald, "
        "else ~/.local/state/herald)",
    )
    json_option = argparse.ArgumentParser(add_help=False)
    json_option.add_argument("--json", action="store_true", help="print JSON, for a program to read")

    parser = argparse.ArgumentParser(
        prog="herald",
        description="Post one announcement to every channel you choose, after showing exactly what each one will get.",
    )
    parser.add_argument("--version", action="version", version=f"herald {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name: str, run: Callable[[argparse.Namespace], int], summary: str, *parents: argparse.ArgumentParser):
        sub = commands.add_parser(name, parents=list(parents), help=summary, description=summary)
        sub.set_defaults(run=run)
        return sub

    command("channels", _channels, "list the channels you can post to, and where each comes from", json_option)

    sub = command(
        "preview",
        _preview,
        "fit an announcement to each channel and show exactly what it will get; sends nothing",
        ledger_option,
        json_option,
    )
    sub.add_argument(
        "file",
        nargs="?",
        help="a text file holding the announcement, or a .json file with text, link, title, id and channels; "
        "- reads standard input",
    )
    sub.add_argument("--to", action="append", metavar="CHANNEL", help="a channel to post to; repeat it, or use commas")
    sub.add_argument("--text", help="the announcement's text")
    sub.add_argument("--link", help="a link to add to the text where it is missing")
    sub.add_argument("--title", help="the announcement's title")
    sub.add_argument(
        "--id",
        help="what makes the announcement post once, such as a release tag (default: derived from the link and title)",
    )

    sub = command("post", _post, "post a draft that preview made, once per channel", ledger_option, json_option)
    sub.add_argument("draft_id", metavar="DRAFT_ID", help="the id preview printed")

    sub = command("check", _check, "check each channel's credentials, without showing any value", json_option)
    sub.add_argument("--to", action="append", metavar="CHANNEL", help="a channel to check (default: every one)")

    sub = command(
        "status", _status, "show what happened to each announcement on each channel", ledger_option, json_option
    )
    sub.add_argument("announcement_id", nargs="?", metavar="ANNOUNCEMENT_ID", help="only this announcement")

    sub = command(
        "resolve", _resolve, "record what you found for a post whose result was never recorded", ledger_option
    )
    sub.add_argument("announcement_id", metavar="ANNOUNCEMENT_ID")
    sub.add_argument("channel", metavar="CHANNEL")
    found = sub.add_mutually_exclusive_group(required=True)
    found.add_argument("--posted", action="store_true", help="the post is there")
    found.add_argument("--not-posted", action="store_true", help="the post is not there, so a later post may send it")
    sub.add_argument("--url", default="", help="the post's link, with --posted")

    sub = command(
        "auth",
        _auth,
        "sign in to a platform whose credential comes from a sign-in (linkedin), and hand over the token",
    )
    sub.add_argument("channel", metavar="CHANNEL")
    sub.add_argument(
        "--output",
        type=Path,
        metavar="FILE",
        help="write the settings to FILE, readable by you alone; without it the token goes to standard output, "
        "which must be a pipe",
    )
    sub.add_argument("--port", type=int, default=8765, help="the local port the sign-in returns to (default: 8765)")
    sub.add_argument("--no-browser", action="store_true", help="print the sign-in address instead of opening it")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except KeyError as error:
        print(f"herald: {error.args[0] if error.args else error}", file=sys.stderr)
    except (TooLong, UnknownDraft, UnknownChannel, ChannelConflict, LedgerBusy, LookupError, ValueError) as error:
        print(f"herald: {error}", file=sys.stderr)
    except OSError as error:
        where = f": {error.filename}" if error.filename else ""
        print(f"herald: {error.strerror or error}{where}", file=sys.stderr)
    return 1


__all__ = ["build_parser", "main"]
