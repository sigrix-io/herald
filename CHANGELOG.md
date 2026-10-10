# Changelog

Herald follows [semantic versioning](https://semver.org). Until 1.0, a minor version may change
the channel interface or the ledger's interface, and its entry here says how.

## 0.1.0 (unreleased)

The first release.

- Announcements: text, a link, a title, text of its own for any channel, and an id that makes
  each one post once.
- Fit and preview: each channel's exact text, counted the way its platform counts and refused
  with its count when it is too long. A preview makes no network call.
- Post: sends only a draft a preview made, once per announcement and channel.
- The ledger interface, and a JSON-lines ledger as the default. A post whose result went
  unrecorded is shown for a person to check, never retried on its own.
- Channels as plugins under the `herald.channels` entry point group, with the `stdout` channel.
- The `herald` command: `channels`, `preview`, `post`, `check`, `status` and `resolve`.
