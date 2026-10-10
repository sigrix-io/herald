"""X: a post through X's API, with the account's own keys, paid for from prepaid credits.

X's API has no free tier. Each post is paid for from credits bought in X's developer console:
$0.015, or $0.20 when the text holds a link, at its prices of April 2026. So the channel stays
off until someone sets its four keys, and ``herald check`` says what a post costs instead of
asking X, which bills every call to its API.

The keys are the app's API key and secret and the account's access token and secret, from the
app's "Keys and tokens" page once its permissions allow reading and writing. Each request is
signed with OAuth 1.0a (RFC 5849). Those tokens do not lapse, so nothing has to be renewed.

X counts a post out of 280 as twitter-text 3 does, the library X's documentation names, and so
does this module. A character in its ranges, most scripts' letters and the common punctuation,
weighs 1, and any other character 2, Chinese, Japanese and Korean among them. An emoji weighs 2
whatever it is made of. A link weighs 23 however long it is, and is found where twitter-text
finds one: with a scheme or without (``example.com``), after any character but a letter or a
digit, in any script, and under a top-level domain on twitter-text's lists. Text is counted
after NFC normalisation.

One thing counts high here: an emoji of several code points that twitter-text's list holds and
this module does not, such as a family or a profession joined by U+200D, or a flag spelled with
tags. It weighs what its code points weigh apart, more than X's 2. Every emoji of one code point,
every keycap and flag, and every emoji with a skin tone or U+FE0F weighs what X weighs it.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import re
import secrets
import time
import unicodedata
import urllib.parse
from collections.abc import Callable, Iterable, Mapping

from herald import http
from herald.channels import Channel, CheckResult, PostResult, x_data
from herald.counting import clusters

API = "https://api.x.com"
#: What a post cost at X's prices of April 2026, for ``herald check`` to say. X's pricing page
#: (docs.x.com/x-api/getting-started/pricing) has today's.
PRICE = "$0.015 a post, or $0.20 with a link"
#: What a link weighs, however long it is: X wraps every link in its 23-character short link.
LINK_WEIGHT = 23

#: twitter-text's third configuration: code points in these ranges weigh 1, all others 2.
_LIGHT = ((0x0000, 0x10FF), (0x2000, 0x200D), (0x2010, 0x201F), (0x2032, 0x2037))
#: The only emoji whose code points weigh 1. X weighs them 2, like any other emoji.
_LIGHT_EMOJI = frozenset("\u00a9\u00ae")

# Links, found as twitter-text's extractor finds them.

#: What may not be in a domain's name: twitter-text's punctuation, spaces, invalid characters and
#: direction marks. Anything else may, in any script, emoji included.
_NOT_IN_NAMES = frozenset(
    "!'#%&()*+,-./:;<=>?@[\\]^_{|}~$"
    + "".join(map(chr, [*range(0x09, 0x0E), 0x20, 0x85, 0xA0, 0x1680, 0x180E, *range(0x2000, 0x200B)]))
    + "".join(map(chr, [0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFFFE, 0xFEFF, 0xFFFF]))
    + "".join(map(chr, [*range(0x202A, 0x202F), 0x061C, 0x200E, 0x200F, *range(0x2066, 0x206A)]))
)
#: A subdomain may also hold underscores and hyphens, and a domain's own name hyphens, though
#: neither at its start or end.
_NOT_IN_LABELS = _NOT_IN_NAMES - {"_", "-"}
#: What may come just before a link: anything but a letter, a digit, @, $ or #. Read ignoring
#: case, as the pattern is, that also rules out what folds to a letter, such as the long s, U+017F.
_BEFORE = re.compile(r"[^A-Za-z0-9@\uff20$#\uff03\ufffe\ufeff\uffff]", re.IGNORECASE)
_SCHEME = re.compile(r"https?://", re.IGNORECASE)
_PORT = re.compile(r":[0-9]+")
_LATIN = (
    r"\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff\u0100-\u024f\u0253\u0254\u0256\u0257\u0259\u025b\u0263"
    r"\u0268\u026f\u0272\u0289\u028b\u02bb\u0300-\u036f\u1e00-\u1eff"
)
_PATH_CHAR = rf"[a-z\u0400-\u04ff0-9!*';:=+,.$/%#\[\]\-\u2013_~@|&{_LATIN}]"
_PARENS = rf"\((?:{_PATH_CHAR}+|{_PATH_CHAR}*\({_PATH_CHAR}+\){_PATH_CHAR}*)\)"
_PATH_END = rf"(?:[+\-a-z\u0400-\u04ff0-9=_#/{_LATIN}]|{_PARENS})"
_PATH = re.compile(rf"/(?:{_PATH_CHAR}*(?:{_PARENS}{_PATH_CHAR}*)*{_PATH_END}|@{_PATH_CHAR}+/)*", re.IGNORECASE)
_QUERY_CHAR = r"[a-z0-9!?*'@();:&=+$/%#\[\]\-_.,~|]"
_QUERY_END = r"[a-z0-9\-_&=#/]"
_QUERY = re.compile(rf"\?{_QUERY_CHAR}*{_QUERY_END}", re.IGNORECASE)
#: X's own short links, which end at their slug and query.
_SHORT_LINK = re.compile(rf"https?://t\.co/([a-z0-9]+)(?:\?{_QUERY_CHAR}*{_QUERY_END})?", re.IGNORECASE)
_LONGEST_SLUG = 40
_LONGEST_LINK = 4096
#: What a name in ASCII and Latin letters holds, where a link without a scheme is looked for.
_ASCII_NAME = re.compile(rf"[\-a-z0-9{_LATIN}]", re.IGNORECASE)


@functools.cache
def _top_level_domain() -> re.Pattern[str]:
    """twitter-text's pattern for a top-level domain, built the first time a link is looked for.

    Of two domains where one starts the other, the longer has to come first for a domain to
    match whole, and sorting them backwards puts it there.
    """

    def names(listed: str) -> str:
        unicode = (name.encode("ascii").decode("idna") if name.startswith("xn--") else name for name in listed.split())
        return "|".join(map(re.escape, sorted(unicode, reverse=True)))

    after = r"(?=[^0-9a-zA-Z@+-]|$)"
    return re.compile(
        rf"(?:{names(x_data.GENERIC_DOMAINS)}){after}|(?:{names(x_data.COUNTRY_DOMAINS)}){after}|xn--[\-0-9a-z]+",
        re.IGNORECASE,
    )


def _runs(text: str, inside: frozenset[str] | re.Pattern[str]) -> list[int]:
    """For each place in ``text``, where the run of characters a label may hold, from there, ends.

    ``inside`` is what a label may not hold, or a pattern for one character it may.
    """
    ends = [len(text)] * (len(text) + 1)
    for place in range(len(text) - 1, -1, -1):
        held = text[place] not in inside if isinstance(inside, frozenset) else inside.match(text, place)
        ends[place] = ends[place + 1] if held else place
    return ends


def _fill_after_dots(text: str, end: Callable[[int], int], found: dict[int, int]) -> None:
    """Put ``end`` for each place just after a dot into ``found``, which ``end`` reads, from the last
    dot back, so that each label is read once."""
    for start in sorted((place + 1 for place, char in enumerate(text) if char == "."), reverse=True):
        found[start] = end(start)


class _Domains:
    """Where a domain starting at any place in a text ends, as twitter-text's pattern finds one.

    The pattern is labels, each ended by a dot, then a top-level domain, and of the ways to read
    it, the one with the most labels. Run as a backtracking pattern, as twitter-text runs it, it
    took half a minute over 4,000 characters of ``a.``, which hold no domain. This reads each
    label, and looks for each top-level domain, once, from the end.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.runs = _runs(text, _NOT_IN_LABELS)
        self.underscores = [0]
        for char in text:
            self.underscores.append(self.underscores[-1] + (char == "_"))
        self.after_dots: dict[int, int] = {}
        _fill_after_dots(text, self._end, self.after_dots)

    def end(self, start: int) -> int:
        """Where the domain starting at ``start`` ends, or -1 when none starts there."""
        known = self.after_dots.get(start)
        return self._end(start) if known is None else known

    def _end(self, start: int) -> int:
        text, dot = self.text, self.runs[start]
        if dot == start or dot >= len(text) or text[dot] != "." or {text[start], text[dot - 1]} & {"_", "-"}:
            return -1
        further = self.after_dots.get(dot + 1, -1)
        if further >= 0:
            return further  # the label is a subdomain of a longer domain
        if self.underscores[dot] > self.underscores[start]:
            return -1  # the domain's own name, before its top-level domain, holds no underscore
        found = _top_level_domain().match(text, dot + 1)
        return found.end() if found else -1


def _ascii_names(domain: str) -> list[tuple[int, int]]:
    """Each name in ASCII and Latin letters inside ``domain``, where a link without a scheme can be.

    twitter-text links ``example.com`` in ``日本example.com`` and leaves the rest as text.
    """
    runs = _runs(domain, _ASCII_NAME)
    reach: dict[int, int] = {}

    def end(start: int) -> int:
        dot = runs[start]
        if dot == start or dot >= len(domain) or domain[dot] != ".":
            return -1
        further = reach.get(dot + 1, -1)
        if further >= 0:
            return further
        found = _top_level_domain().match(domain, dot + 1)
        return found.end() if found else -1

    _fill_after_dots(domain, end, reach)
    names = []
    place = 0
    while place < len(domain):
        found = end(place)
        if found >= 0:
            names.append((place, found))
            place = found
        else:
            place += 1
    return names


def _link_at(text: str, start: int, domains: _Domains) -> tuple[int, str, str, bool] | None:
    """The link starting at ``start``, if one does: where it ends, its scheme, its domain, and
    whether a path follows the domain."""
    scheme = _SCHEME.match(text, start)
    for domain_start in (scheme.end(), start) if scheme else (start,):
        domain_end = domains.end(domain_start)
        if domain_end < 0:
            continue
        end = domain_end
        port = _PORT.match(text, end)
        if port:
            end = port.end()
        path = _PATH.match(text, end)
        if path:
            end = path.end()
        query = _QUERY.match(text, end)
        if query:
            end = query.end()
        return end, text[start:domain_start], text[domain_start:domain_end], path is not None
    return None


def _valid(url: str, scheme: str, domain: str) -> bool:
    """Whether a link's domain has an ASCII form, and the link, with it, is no longer than X takes."""
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    return bool(ascii_domain) and len(scheme) + len(url) + len(ascii_domain) - len(domain) <= _LONGEST_LINK


def links(text: str) -> dict[int, int]:
    """Where X finds links in ``text``, NFC-normalised: each link's start, and how long it is.

    Each is found as twitter-text's extractor finds it, where that differs from what a reader would
    call the link too: a name without a scheme takes its port into the link only with a path.
    """
    if "." not in text:
        return {}
    domains = _Domains(text)
    found: dict[int, int] = {}
    place = 0
    while place < len(text):
        link = None
        if _BEFORE.match(text, place):
            start, before = place + 1, text[place]
            link = _link_at(text, start, domains)
        if link is None and place == 0:
            start, before = 0, ""
            link = _link_at(text, start, domains)
        if link is None:
            place += 1
            continue
        end, scheme, domain, has_path = link
        place = end
        url = text[start:end]
        if not _valid(url, scheme or "https://", domain):
            continue
        if scheme:
            short = _SHORT_LINK.match(url)
            if short:
                if len(short.group(1)) > _LONGEST_SLUG:
                    continue
                url = short.group()
            found[start] = len(url)
            continue
        if before in ("-", "_", ".", "/"):
            continue  # a name without a scheme, joined to the word before it, is not a link
        last = None
        for name_start, name_end in _ascii_names(domain):
            last = (start + name_start, domain[name_start:name_end])
            found[last[0]] = name_end - name_start
        if last is not None and has_path:
            found[last[0]] = len(url.replace(domain, last[1]))
    return found


# Emoji, and the rest of the text.


def _code_points(listed: str) -> frozenset[str]:
    """The characters a list of hex code points and ranges names, such as ``"A9 2194-2199"``."""
    chars: set[str] = set()
    for item in listed.split():
        first, _, last = item.partition("-")
        chars.update(map(chr, range(int(first, 16), int(last or first, 16) + 1)))
    return frozenset(chars)


_TEXT_STYLE = _code_points(x_data.TEXT_STYLE_EMOJI)
_SKIN_TONED = _code_points(x_data.SKIN_TONE_EMOJI)
_SKIN_TONES = _code_points("1F3FB-1F3FF")
#: Each flag as its two regional indicators, which run from U+1F1E6 for A.
_FLAGS = frozenset(chr(0x1F1A5 + ord(first)) + chr(0x1F1A5 + ord(second)) for first, second in x_data.FLAGS.split())
_KEYCAPS = frozenset(base + mark for base in "0123456789#*" for mark in ("\u20e3", "\ufe0f\u20e3"))


def _one_emoji(cluster: str) -> bool:
    """Whether X's list holds ``cluster``, of more than one code point, as one emoji."""
    if cluster in _KEYCAPS or cluster in _FLAGS:
        return True
    if len(cluster) != 2:
        return False
    base, mark = cluster
    return (mark == "\ufe0f" and base in _TEXT_STYLE) or (mark in _SKIN_TONES and base in _SKIN_TONED)


def _char_weight(char: str) -> int:
    if char in _LIGHT_EMOJI:
        return 2
    code = ord(char)
    return 1 if any(first <= code <= last for first, last in _LIGHT) else 2


def _weight(text: str) -> int:
    """What text with no link in it weighs: 2 for each emoji X's list holds whole, else what each
    code point weighs, which is never less than X's, however X divides the code points into emoji."""
    return sum(
        2 if len(cluster) > 1 and _one_emoji(cluster) else sum(map(_char_weight, cluster)) for cluster in clusters(text)
    )


def weighted_length(text: str) -> int:
    """How long ``text`` is to X: its weighted length, which X holds to 280."""
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    found = links(text)
    total = gap = place = 0
    while place < len(text):
        length = found.get(place)
        if length is None:
            place += 1
            continue
        total += _weight(text[gap:place]) + LINK_WEIGHT
        place = gap = place + length
    return total + _weight(text[gap:])


def _percent(value: str) -> str:
    """Percent-encoding as OAuth 1.0a signs with it: everything but RFC 3986's unreserved characters."""
    return urllib.parse.quote(value, safe="-._~")


def signed_header(
    method: str,
    url: str,
    *,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
    form: Iterable[tuple[str, str]] = (),
    nonce: str = "",
    timestamp: str = "",
) -> str:
    """The Authorization header for a request signed with OAuth 1.0a's HMAC-SHA1 (RFC 5849).

    The signature covers the method, the address with its query and ``form``, a form-encoded
    body's fields. A JSON body is not signed, so a post to X passes none. ``nonce`` and
    ``timestamp`` are for tests; left empty, each request gets fresh ones.
    """
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    pairs = sorted((_percent(name), _percent(value)) for name, value in [*oauth.items(), *query, *form])
    base = "&".join(
        (
            method.upper(),
            _percent(f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}"),
            _percent("&".join(f"{name}={value}" for name, value in pairs)),
        )
    )
    key = f"{_percent(consumer_secret)}&{_percent(token_secret)}"
    digest = hmac.new(key.encode("utf-8"), base.encode("utf-8"), hashlib.sha1).digest()
    oauth["oauth_signature"] = base64.b64encode(digest).decode("ascii")
    return "OAuth " + ", ".join(f'{_percent(name)}="{_percent(value)}"' for name, value in sorted(oauth.items()))


class XChannel(Channel):
    name = "x"
    label = "X"
    limit = 280
    credentials = ("HERALD_X_API_KEY", "HERALD_X_API_SECRET", "HERALD_X_ACCESS_TOKEN", "HERALD_X_ACCESS_TOKEN_SECRET")

    def count(self, text: str) -> int:
        return weighted_length(text)

    def _authorization(self, method: str, url: str, env: Mapping[str, str]) -> str:
        return signed_header(
            method,
            url,
            consumer_key=self.setting(env, "HERALD_X_API_KEY"),
            consumer_secret=self.setting(env, "HERALD_X_API_SECRET"),
            token=self.setting(env, "HERALD_X_ACCESS_TOKEN"),
            token_secret=self.setting(env, "HERALD_X_ACCESS_TOKEN_SECRET"),
        )

    def post(self, text: str, env: Mapping[str, str]) -> PostResult:
        url = f"{API}/2/tweets"
        answer = http.post_json(url, {"text": text}, headers={"Authorization": self._authorization("POST", url, env)})
        http.judge(answer, "X")
        data = (answer.json() or {}).get("data") or {}
        post_id = str(data.get("id") or "")
        return PostResult(url=f"https://x.com/i/web/status/{post_id}" if post_id else "", id=post_id)

    def check(self, env: Mapping[str, str]) -> CheckResult:
        missing = self.missing_credentials(env)
        if missing:
            return CheckResult(False, f"set {', '.join(missing)}; posting to X is paid: {PRICE}, from prepaid credits")
        return CheckResult(
            True,
            f"credentials are set, not checked with X, which bills every call to its API; "
            f"posting is paid: {PRICE}, from prepaid credits",
        )
