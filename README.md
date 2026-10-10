# Herald

Post one announcement to every channel you choose, after showing exactly what each one will get.

Herald fits an announcement to each channel, counted the way that platform counts. It shows you
the exact text for every channel, then posts that text and nothing else, once. A ledger records
every result, so running it twice never posts twice.

It is a command, a Python library and a plugin interface. It uses the standard library only, and
it talks to nothing but the platforms you post to.

```bash
pip install herald-post     # the command it installs is `herald`
```

The PyPI name is `herald-post` because `herald` belongs to another project.

## Try it

The `stdout` channel prints instead of posting, so you can watch the whole flow without an account
anywhere:

```console
$ herald preview --to stdout --text "Version 2.0 is out." --link https://example.com/releases/2.0 --id v2.0
Draft c67df7d7e742, for announcement v2.0. Nothing was sent.

--- stdout: 53, no limit ---
Version 2.0 is out.

https://example.com/releases/2.0

To post exactly this: herald post c67df7d7e742

$ herald post c67df7d7e742
--- stdout ---
Version 2.0 is out.

https://example.com/releases/2.0
--- end ---
stdout: posted

$ herald post c67df7d7e742
stdout: already posted, so not again: posted 2026-10-10T09:00:00+00:00
```

## How it works

1. **Fit.** Each channel counts the text the way its platform does. Bluesky counts graphemes, the
   characters a reader sees. Mastodon and X count every link as 23 characters, however long it
   is. Text over a channel's limit is refused with its count and the limit. Herald never cuts it.
2. **Preview.** `herald preview` shows each channel's exact text and gives the draft an id. It
   makes no network call.
3. **Post.** `herald post` takes only a draft id, so what you saw is what goes out. It posts each
   channel once per announcement and records each result in the ledger.

An announcement has text, an optional link, an optional title, and optional text of its own for
any channel. Its **id** is what makes it post once, so give it whatever already names the thing
you are announcing, such as a release tag or a post's slug. Leave it out and Herald derives the
id from the link and the title. Announce the same thing twice and Herald posts it once.

A JSON file can hold the announcement, and `-` reads one from standard input:

```json
{
  "id": "v2.0",
  "title": "Version 2.0",
  "text": "Version 2.0 is out, with a faster importer and a new export format.",
  "link": "https://example.com/releases/2.0",
  "channels": {
    "bluesky": "Version 2.0 is out: a faster importer and a new export format."
  }
}
```

```bash
herald preview announcement.json --to bluesky,mastodon
```

## Commands

| Command | What it does |
| --- | --- |
| `herald channels` | List the channels you can post to, their limits, the credentials they read and the package each comes from. |
| `herald preview [FILE] --to CHANNEL` | Fit an announcement to each channel and show exactly what it will get. Sends nothing. |
| `herald post DRAFT_ID` | Post that draft, once per channel. |
| `herald check [--to CHANNEL]` | Check each channel's credentials, naming any that are missing and showing no value. |
| `herald status [ANNOUNCEMENT_ID]` | Show what happened to each announcement on each channel. |
| `herald resolve ANNOUNCEMENT_ID CHANNEL --posted/--not-posted` | Record what you found on a platform after a post's result went unrecorded. |

Every command takes `--json` for a program to read, except `resolve`. Exit status is 0 when the
command did what it was asked and 1 when it did not, for example a channel not posted, text too
long or a check that failed. Exit status 2 means the command itself was wrong.

## The ledger

The ledger keeps every draft a preview made and what happened to each announcement on each
channel: *drafted*, then *sending*, then *posted* or *failed*. By default it lives in
`~/.local/state/herald`. Set `$XDG_STATE_HOME` or `$HERALD_LEDGER_DIR`, or pass `--ledger-dir`,
to keep it elsewhere, such as in a directory a CI job caches between runs.

A post is recorded as *sending* before the platform is asked. If Herald is killed after asking
and before recording the answer, the entry stays in *sending*, and the platform may have the
post. Herald never retries such an entry on its own, because that could post twice. `herald
status` shows the entry. You check the platform, then record what you found:

```bash
herald resolve v2.0 mastodon --posted --url https://mastodon.social/@you/123
herald resolve v2.0 mastodon --not-posted     # a later `herald post` may send it
```

A post the platform refused, saying so, is *failed*, and the next `herald post` tries it again.

## Credentials

Each channel reads its credentials from environment variables it names; `herald channels` lists
them. Herald never stores, prints or logs a credential. `herald check` names a missing variable
without showing any value. A channel's error messages are checked before they are shown or
recorded, and any credential in them is replaced by its variable's name. A webhook's URL counts
as a credential, so its path is replaced too.

## Channels

This is Herald's core, with the `stdout` channel. Channels for Bluesky, Mastodon, Discord, Slack,
Telegram and LinkedIn profiles are being written to arrive with the first release.

A channel is a class, and a package registers it under the `herald.channels` entry point group.
The built-in channels register the same way, so a channel from your own package is as much a
part of Herald as they are:

```python
import json
import urllib.error
import urllib.request
from collections.abc import Mapping

from herald import Channel, NotPosted, PostResult


class Chatroom(Channel):
    name = "chatroom"
    label = "Our chat room"
    limit = 2000
    credentials = ("CHATROOM_WEBHOOK_URL",)

    def post(self, text: str, env: Mapping[str, str]) -> PostResult:
        body = json.dumps({"text": text}).encode()
        request = urllib.request.Request(
            env["CHATROOM_WEBHOOK_URL"], data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30):
                return PostResult()
        except urllib.error.HTTPError as error:
            if 400 <= error.code < 500:
                raise NotPosted(f"the chat room refused the post with {error.code}") from None
            raise
```

```toml
[project.entry-points."herald.channels"]
chatroom = "your_package.channels:Chatroom"
```

Raise `NotPosted` only when the platform's own answer says nothing was posted. Any other
exception, including a timeout after the request was sent, leaves the post's fate unknown, and
Herald then waits for a person rather than risk posting twice. A channel that counts differently
overrides `count`; `herald.counting` has the pieces: `graphemes`, which only ever errs high, and
`with_links_as`. An application that does not want to package a channel can call
`herald.register(Chatroom)` instead.

## As a library

```python
from herald import Announcement, JsonLinesLedger, post, preview

ledger = JsonLinesLedger("herald-state")
announcement = Announcement(text="Version 2.0 is out.", link="https://example.com/releases/2.0", id="v2.0")

draft = preview(announcement, ["stdout"], ledger)  # raises herald.TooLong, with counts, when text does not fit
for item in draft.items:
    print(item.channel, item.count, item.limit, item.text, sep="\n")

for outcome in post(draft.id, ledger):
    print(outcome.channel, outcome.status, outcome.url)
```

An application with a database of its own can implement `herald.Ledger` over it. Its `claim`
must be atomic: of two processes claiming the same announcement and channel, only one may post.

## The rules it keeps

- **Nobody's defaults.** Herald names no service, account or address of its own. It talks only to
  the platforms you post to, and it collects nothing.
- **Credentials by name,** from the environment, never stored, printed or logged.
- **Preview before post.** A post sends only a draft a preview made.
- **Never twice.** The ledger keys on each announcement's id.
- **Refuse, never cut.** An over-long post comes back with its count.
- **Plugins are first-class.** The built-in channels use the same interface as anyone's.
- **No dependencies.** The standard library only, from Python 3.11.

## Where it fits

Herald is one of [Sigrix](https://sigrix.io)'s open-source projects, and nothing in it is specific
to Sigrix. Its neighbours on the map are planned rather than built: sigrix.io will announce each
update it publishes on its Hub through Herald, keeping Herald's ledger in its own store, and
Bailey, Sigrix's assistant, will reach Herald through Herald's MCP server. The rest of the family,
and how it fits together, is at [sigrix.io/open-source](https://sigrix.io/open-source).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
