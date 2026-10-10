# Herald

Post one announcement to every channel you choose, after showing exactly what each one will get.

Herald fits an announcement to each channel, counted the way that platform counts. It shows you
the exact text for every channel, then posts that text and nothing else, once. A ledger records
every result, so running it twice never posts twice.

It is a command, a GitHub Action, a Python library, an MCP server and a plugin interface. It uses
the standard library only, and it talks to nothing but the platforms you post to and the feeds you
watch.

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
| `herald watch FEED --to CHANNEL` | Post each new item of an RSS or Atom feed, such as a repository's releases, once. |
| `herald check [--to CHANNEL]` | Check each channel's credentials, naming any that are missing and showing no value. |
| `herald status [ANNOUNCEMENT_ID]` | Show what happened to each announcement on each channel. |
| `herald resolve ANNOUNCEMENT_ID CHANNEL --posted/--not-posted` | Record what you found on a platform after a post's result went unrecorded. |
| `herald auth CHANNEL` | Sign in to LinkedIn in the browser, for `linkedin` or `linkedin-page`, and hand over the token, to a pipe or a file only. |
| `herald mcp [--http]` | Serve `channels`, `preview`, `post` and `check` to an assistant or a workflow tool, over MCP. |

Every command takes `--json` for a program to read, except `resolve`, `auth` and `mcp`. Exit
status is 0 when the command did what it was asked and 1 when it did not, for example a channel
not posted, text too long or a check that failed. Exit status 2 means the command itself was wrong.

## The ledger

The ledger keeps every draft a preview made and what happened to each announcement on each
channel: *drafted*, then *sending*, then *posted* or *failed*. For `herald watch`, it also keeps
what each feed held when it was first watched into each channel. By default it lives in
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
as a credential, so its path is replaced too. Every request goes over https, and a message names
a platform's host, never a path, since a path can carry a token.

`herald auth` is the one command that hands over a credential, because signing in is how LinkedIn
gives one. It writes the token to a pipe or to a file you name, created readable by you
alone, and refuses to print it on a terminal.

## Channels

| Channel | Longest post | Counted as | Variables |
| --- | --- | --- | --- |
| `bluesky` | 300 | graphemes, and 3,000 bytes | `HERALD_BLUESKY_HANDLE`, `HERALD_BLUESKY_APP_PASSWORD` |
| `mastodon` | 500, or the server's | graphemes, a link as 23 | `HERALD_MASTODON_SERVER`, `HERALD_MASTODON_TOKEN` |
| `discord` | 2,000 | UTF-16 code units | `HERALD_DISCORD_WEBHOOK_URL` |
| `slack` | 40,000 | UTF-16 code units, escaped | `HERALD_SLACK_WEBHOOK_URL` |
| `telegram` | 4,096 | UTF-16 code units | `HERALD_TELEGRAM_BOT_TOKEN`, `HERALD_TELEGRAM_CHAT_ID` |
| `linkedin` | 3,000 | UTF-16 code units, escaped | `HERALD_LINKEDIN_TOKEN`, from `herald auth linkedin` |
| `linkedin-page` | 3,000 | UTF-16 code units, escaped | `HERALD_LINKEDIN_PAGE_ORGANIZATION`, with `HERALD_LINKEDIN_PAGE_REFRESH_TOKEN` from `herald auth linkedin-page` and the app's `HERALD_LINKEDIN_PAGE_CLIENT_ID` and `HERALD_LINKEDIN_PAGE_CLIENT_SECRET`, or `HERALD_LINKEDIN_PAGE_TOKEN` alone |
| `x` | 280 | X's weights, a link as 23 | `HERALD_X_API_KEY`, `HERALD_X_API_SECRET`, `HERALD_X_ACCESS_TOKEN`, `HERALD_X_ACCESS_TOKEN_SECRET` |
| `stdout` | none | | none |

Where a platform counts in a way the standard library cannot match exactly, Herald counts high, so
text it lets through is never text the platform refuses for its length.

**Bluesky.** Create an app password under Settings, Privacy and security, App passwords; never use
the account's own password. Each link becomes a facet, which is what makes it clickable. For an
account Bluesky does not host, set `HERALD_BLUESKY_SERVICE` to its server.

**Mastodon.** Under Preferences, Development, create an application with the `write:statuses`
scope, and `read:accounts` for `herald check`, and copy its access token. `HERALD_MASTODON_SERVER`
is the server's address. A preview cannot ask the server for its limit, so Herald holds posts to
500 characters unless `HERALD_MASTODON_MAX_CHARACTERS` says otherwise, and `herald check` reads
the server's limit and says when the two differ. `HERALD_MASTODON_VISIBILITY` may be `public` (the
default), `unlisted`, `private` or `direct`. Each post carries an Idempotency-Key, so the same text
sent twice within an hour is posted once.

**Discord.** In the channel's settings, under Integrations, Webhooks, create a webhook and copy
its URL. The message allows no mentions, so an announcement that says @everyone pings nobody.

**Slack.** In a Slack app, turn on Incoming Webhooks and add one for the channel. `herald check`
sends an empty message, which a working webhook refuses without posting.

**Telegram.** Create a bot with @BotFather, add it to the channel as an admin that can post, and
use the channel's @username or numeric id as the chat. `HERALD_TELEGRAM_API` names a self-hosted
Bot API server.

**LinkedIn.** This posts as a person, on their profile. At linkedin.com/developers, create an app
with the products "Share on LinkedIn" and "Sign In with LinkedIn using OpenID Connect", and add
`http://localhost:8765/callback` as a redirect URL. Then sign in:

```bash
export HERALD_LINKEDIN_CLIENT_ID=... HERALD_LINKEDIN_CLIENT_SECRET=...
herald auth linkedin --output ~/.config/herald/linkedin.env     # a file readable by you alone
herald auth linkedin | gh secret set HERALD_LINKEDIN_TOKEN      # or straight into a secret store
```

The token lasts 60 days and LinkedIn will not refresh it, so `herald check` warns two weeks before
it lapses; sign in again then. The file also holds `HERALD_LINKEDIN_AUTHOR`, which saves asking
LinkedIn who the token belongs to, and `HERALD_LINKEDIN_TOKEN_EXPIRES`, which the warning reads.

**LinkedIn page.** This posts as an organisation, on its page. It needs LinkedIn's Community
Management API, which LinkedIn grants only after reviewing the company that asks, and only to an
app with no other product, so the page needs an app of its own. At linkedin.com/developers, create
one, verified by the page, request the Community Management API, and add
`http://localhost:8765/callback` as a redirect URL. Once LinkedIn grants it, someone who is the
page's administrator, content admin or direct sponsored content poster signs in:

```bash
export HERALD_LINKEDIN_PAGE_CLIENT_ID=... HERALD_LINKEDIN_PAGE_CLIENT_SECRET=...
herald auth linkedin-page --output ~/.config/herald/linkedin-page.env
```

`HERALD_LINKEDIN_PAGE_ORGANIZATION` is the page's number, from its admin address,
linkedin.com/company/NUMBER/admin. LinkedIn gives an approved app a refresh token that lasts a
year from the sign-in, and each post renews an access token from it, so the page posts all year
with nobody signing in again. Renewing needs the app's id and secret, so keep them set beside the
refresh token. `herald check` renews once to see that the refresh token still works, and warns two
weeks before its year is up. An app LinkedIn gives no refresh token gets an access token that
lasts 60 days, saved as `HERALD_LINKEDIN_PAGE_TOKEN`, and the channel posts with that when it is
set.

**X.** X's API has no free tier: each post is paid for from credits bought in X's developer
console, $0.015 a post or $0.20 when the post holds a link, at X's prices of April 2026. So nothing
is posted to X until its four keys are set, and `herald check` says what a post costs instead of
asking X, which bills every call to its API. In the developer console, create an app and set its
permissions to read and write. Then, on the app's Keys and tokens page, generate its API key and
secret and your access token and secret. Generate the access token after setting the permissions,
since a token keeps the permissions it was made with. The keys do not lapse.

X counts a post out of 280 as twitter-text does, the library X's documentation names, and Herald
counts it the same way. Most letters and punctuation weigh 1. Chinese, Japanese and Korean
characters and every emoji weigh 2. A link weighs 23, and Herald finds links where twitter-text
does: with or without `https://`, straight after a word in Japanese, and only under a top-level
domain on twitter-text's lists. An emoji joined from others by U+200D, such as a family, is one
emoji to X, and weighs here what its parts weigh, so it counts high.

### What a failed post means

A post the platform refused, a 4xx answer, was not made, and neither was one whose request never
left: the name did not resolve, the connection was refused or TLS failed. Herald records those as
*failed*, and the next `herald post` tries again. A 5xx answer, a redirect, or no answer at all
after the request was sent leaves the post's fate unknown, so it stays in *sending* for a person
to check. A step before the post itself, such as signing in to Bluesky, can fail any way it likes:
nothing was posted.

### Writing a channel

A channel is a class, and a package registers it under the `herald.channels` entry point group.
The built-in channels register the same way, so a channel from your own package is as much a part
of Herald as they are. `herald.http` sends over https and reads an answer the way the built-in
channels do:

```python
from collections.abc import Mapping

from herald import Channel, PostResult, http


class Chatroom(Channel):
    name = "chatroom"
    label = "Our chat room"
    limit = 2000
    credentials = ("CHATROOM_WEBHOOK_URL",)

    def post(self, text: str, env: Mapping[str, str]) -> PostResult:
        answer = http.post_json(self.setting(env, "CHATROOM_WEBHOOK_URL"), {"text": text})
        http.judge(answer, "The chat room")  # 4xx: not posted; anything else but 2xx: uncertain
        return PostResult()
```

```toml
[project.entry-points."herald.channels"]
chatroom = "your_package.channels:Chatroom"
```

Raise `NotPosted` only when nothing was posted. Any other exception leaves the post's fate
unknown, and Herald then waits for a person rather than risk posting twice. A channel that counts
differently overrides `count`; `herald.counting` has the pieces: `graphemes`, which only ever
errs high, `clusters`, the characters it counts, `utf16_length` and `with_links_as`. A channel that can ask its platform whether its
credentials work overrides `check`. An application that does not want to package a channel can
call `herald.register(Chatroom)` instead.

## Watching a feed

`herald watch` posts each new item of an RSS or Atom feed once. A repository's releases are a feed,
at `https://github.com/OWNER/REPO/releases.atom`:

```bash
herald watch https://github.com/octo/widget/releases.atom --to bluesky,mastodon
```

The first run for a feed and a channel records the items the feed holds and posts none of them, so
turning a watch on never floods a channel with old items, and adding a channel later never floods
that one. Each run after that posts what the feed gained, oldest first. An item is known by its own
id, its Atom id or RSS guid, and becomes an announcement with that id, so the ledger posts it once,
and a post that failed is tried again on the next run, as any post is.

A post says the item's title, and its link is added. `--text` says something else, with `{title}`,
`{link}` and `{feed}`, the feed's title, filled in: `--text "{feed}: {title}"`. A run that finds
more new items than `--most`, 5 unless you say, posts none of them and exits 1, because that many at
once usually means the feed changed its items' ids and they are old ones. Run it again with
`--mark-seen` to record them all as seen, or with a larger `--most` to post them.

A feed is read over https or from a file, never over plain http, so that nobody on the way can
change what gets posted. A feed that declares a DOCTYPE is refused: no feed needs one, and one can
hide an XML attack.

## As a GitHub Action

The action runs `herald watch` and keeps the ledger in the Actions cache between runs, so a
repository announces each of its releases once, on a schedule. GitHub-hosted runners cost a public
repository nothing. Put each channel's credentials in the repository's secrets, and save this as
`.github/workflows/announce.yml`:

```yaml
name: Announce releases

on:
  schedule:
    - cron: "17 * * * *"  # every hour
  workflow_dispatch:

permissions:
  contents: read

# One run at a time: two at once could both post a release.
concurrency:
  group: announce
  cancel-in-progress: false

jobs:
  announce:
    runs-on: ubuntu-latest
    steps:
      - uses: sigrix-io/herald@v0.1.0
        with:
          to: bluesky,mastodon
        env:
          HERALD_BLUESKY_HANDLE: ${{ secrets.HERALD_BLUESKY_HANDLE }}
          HERALD_BLUESKY_APP_PASSWORD: ${{ secrets.HERALD_BLUESKY_APP_PASSWORD }}
          HERALD_MASTODON_SERVER: ${{ secrets.HERALD_MASTODON_SERVER }}
          HERALD_MASTODON_TOKEN: ${{ secrets.HERALD_MASTODON_TOKEN }}
```

The first run records the releases there are and posts none. The first run after a new release
posts it, and the next posts nothing. With `to: stdout`, a run shows its posts in its log instead.

| Input | What it is | Default |
| --- | --- | --- |
| `to` | The channels to post to, separated by commas. | none: give it |
| `feed` | The feed to watch, an https address or a file. | this repository's `releases.atom` |
| `text` | What each post says, with `{title}`, `{link}` and `{feed}` filled in. | `{title}` |
| `most` | The most items one run posts. | `5` |
| `ledger` | The name the ledger is cached under. Give each watch in a repository its own. | `herald` |

What the cache means for a watch:

- Runs share the ledger only through the cache, so let one run at a time, as `concurrency` does
  above.
- Each git ref keeps a ledger of its own. Run the workflow on a schedule, or by hand from the
  default branch. A run on another ref, such as a release's tag, finds no ledger, so it records
  what the feed holds and posts nothing. That is why the workflow above is not started by
  `release` events.
- GitHub drops a cache nobody has read for 7 days, so run the workflow at least daily. After a
  longer gap, a run starts over: it records what the feed holds and posts nothing.
- GitHub pauses the schedule of a public repository that has seen no activity for 60 days.
- If the ledger cannot be saved after a run, the run fails, since the next one would not know what
  this one posted. Check the platforms before the next run.

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

## As an MCP server

`herald mcp` serves Herald to an assistant or a workflow tool over the Model Context Protocol, as
four tools:

| Tool | What it does |
| --- | --- |
| `channels` | Lists the channels Herald can post to, with each one's limit and any credential not set. |
| `preview` | Fits an announcement to each channel and returns a draft id with each channel's exact text. Takes `channels` and `text`, and optionally `link`, `title`, `id` and `channel_texts`, text of a channel's own. Sends nothing. |
| `post` | Posts a draft. It takes the draft id and nothing else, and refuses a call without one. |
| `check` | Checks each channel's credentials, naming any that are missing and showing no value. |

Since `post` takes only a draft id that `preview` returned, a model posts exactly the text a
preview showed. The tools tell the model to show the person that text and to post only once they
agree, and posting the same announcement twice posts it once. Each result comes as text, for a
model to read, and, to a client of MCP 2025-06-18 or later, as `structuredContent` too, for a
workflow. The server keeps drafts and results in the same ledger as the command, so `herald
status` shows what an assistant posted.

**Claude Code** starts it on your machine and talks to it over standard input and output:

```bash
claude mcp add herald -- herald mcp
```

Claude Code starts the server with its own environment, so the channel credentials set in the
shell you start Claude Code from reach Herald. `claude mcp add herald -e NAME=value -- herald mcp`
keeps one in Claude Code's configuration instead. Any assistant that starts an MCP server as a
command runs Herald the same way.

**n8n**, and any tool that reaches a server by its address, uses HTTP:

```bash
export HERALD_MCP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
herald mcp --http     # http://127.0.0.1:8766/mcp
```

In n8n, an MCP Client node, or an MCP Client Tool under an AI Agent, connects to that endpoint with
the HTTP Streamable transport and Bearer Auth holding the token. A node that previews passes
`{{ $json.structuredContent.draft_id }}` to the node that posts, as its `draft_id`. An n8n with
`N8N_SSRF_PROTECTION_ENABLED` set refuses private addresses such as this one until
`N8N_SSRF_ALLOWED_HOSTNAMES` or `N8N_SSRF_ALLOWED_IP_RANGES` allows it.

The server listens on 127.0.0.1 unless `--host` names another address. When `HERALD_MCP_TOKEN`
is set, every request must carry it as `Authorization: Bearer <token>`, and without it the server
will not listen beyond this machine at all. So when n8n runs in a container or on another machine,
start Herald with the token and with `--host` set to an address n8n can reach. The token comes
from the environment, never from an option, because anyone on the machine can read a command's
options. The server refuses every request a browser page makes, which keeps a web page from
reaching it through DNS rebinding.

Herald speaks MCP revisions 2024-11-05 to 2025-11-25, which open with a handshake. A client of
2026-07-28 asks in that revision first, is refused, and falls back to the handshake, as 2026-07-28
provides.

## The rules it keeps

- **Nobody's defaults.** Herald names no service, account or address of its own. It talks only to
  the platforms you post to, over https, and it collects nothing.
- **Credentials by name,** from the environment, never stored, printed or logged.
- **Preview before post.** A post sends only a draft a preview made.
- **Never twice.** The ledger keys on each announcement's id.
- **Refuse, never cut.** An over-long post comes back with its count.
- **Plugins are first-class.** The built-in channels use the same interface as anyone's.
- **No dependencies.** The standard library only, from Python 3.11.

## Where it fits

Herald is one of [Sigrix](https://sigrix.io)'s open-source projects, and nothing in it is specific
to Sigrix. Its neighbour on the map is sigrix.io, which announces each update it publishes on its
Hub through Herald, keeping Herald's ledger in its own store. The rest of the family, and how it
fits together, is at [sigrix.io/open-source](https://sigrix.io/open-source).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
