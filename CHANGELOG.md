# Changelog

Herald follows [semantic versioning](https://semver.org). Until 1.0, a minor version may change
the channel interface or the ledger's interface, and its entry here says how.

## 0.1.0 (2026-10-10)

The first release.

- Announcements: text, a link, a title, text of its own for any channel, and an id that makes
  each one post once.
- Fit and preview: each channel's exact text, counted the way its platform counts and refused
  with its count when it is too long. A preview makes no network call.
- Post: sends only a draft a preview made, once per announcement and channel.
- The ledger interface, and a JSON-lines ledger as the default. A post whose result went
  unrecorded is shown for a person to check, never retried on its own.
- Channels as plugins under the `herald.channels` entry point group.
- The channels: `bluesky`, `mastodon`, `discord`, `slack`, `telegram`, `linkedin` (a person's
  profile) and `stdout`, each counting the way its platform does.
- `herald.http`: requests over https only, never following a redirect, with each failure read
  as not posted (safe to try again) or uncertain (left for a person).
- The `herald` command: `channels`, `preview`, `post`, `check`, `status`, `resolve`, and
  `auth linkedin`, which signs in on localhost and hands the token only to a pipe or a file.
- `herald mcp`: the tools `channels`, `preview`, `post` and `check` over the Model Context
  Protocol, on standard input and output, or over HTTP with `--http`. `post` takes only a draft
  id that `preview` returned. Over HTTP the server listens on 127.0.0.1 unless told otherwise,
  needs a bearer token from `HERALD_MCP_TOKEN` to listen anywhere else, and refuses requests from
  browser pages. It speaks MCP revisions 2024-11-05 to 2025-11-25.
- `herald.check`: each channel's own check of its credentials, for a program.
- `herald watch`: posts each new item of an RSS 2.0, RSS 1.0 or Atom feed, a repository's
  releases included, once. Its first run for a feed and channel records what the feed holds and
  posts none of it, and a run with more new items than `--most` posts none. A feed is read over
  https or from a file only, and one that declares a DOCTYPE is refused. `herald.feeds` holds the
  pieces for a program, and the ledger keeps what each feed held when first watched.
- The GitHub Action, `action.yml`: `herald watch` on a schedule, with the ledger kept in the
  Actions cache per name and git ref.
- A suggested `herald resolve` command quotes an announcement id that a shell would split.
