"""Counting as platforms count, held against a full implementation of Unicode's grapheme clusters.

``regex`` is that implementation. It is a development dependency only: Herald itself never
imports it, which ``test_rules.py`` checks. Test strings are spelled as code points, because most
of what matters here is invisible.
"""

from __future__ import annotations

import itertools
import random

import pytest

from herald.counting import _PICTOGRAPHS, LINK, _extends, clusters, graphemes, with_links_as

regex = pytest.importorskip("regex")
CLUSTER = regex.compile(r"\X")
PICTOGRAPH = regex.compile(r"\p{Extended_Pictographic}")

ZWJ, VS16, KEYCAP, ACUTE = 0x200D, 0xFE0F, 0x20E3, 0x0301


def chars(*codes: int) -> str:
    return "".join(map(chr, codes))


def unicode_count(text: str) -> int:
    return len(CLUSTER.findall(text))


SEQUENCES = {
    "letters": "Hello, world",
    "a combining accent": chars(0x65, ACUTE),
    "a skin tone": chars(0x1F44D, 0x1F3FF),
    "a profession": chars(0x1F469, ZWJ, 0x1F4BB),
    "a family": chars(0x1F468, ZWJ, 0x1F469, ZWJ, 0x1F467, ZWJ, 0x1F466),
    "the rainbow flag": chars(0x1F3F3, VS16, ZWJ, 0x1F308),
    "the pirate flag": chars(0x1F3F4, ZWJ, 0x2620, VS16),
    "a heart on fire": chars(0x2764, VS16, ZWJ, 0x1F525),
    "a toned astronaut": chars(0x1F9D1, 0x1F3FD, ZWJ, 0x1F680),
    "a country flag": chars(0x1F1F5, 0x1F1F1),
    "a subdivision flag": chars(0x1F3F4, 0xE0067, 0xE0062, 0xE0065, 0xE006E, 0xE0067, 0xE007F),
    "a keycap": chars(0x31, VS16, KEYCAP),
    "a shaking head": chars(0x1F642, ZWJ, 0x2194, VS16),
    "a black cat": chars(0x1F408, ZWJ, 0x2B1B),
    "a lime": chars(0x1F34B, ZWJ, 0x1F7E9),
    "three flags": chars(0x1F1F5, 0x1F1F1, 0x1F1FA, 0x1F1F8, 0x1F1E9, 0x1F1EA),
    "a flag and a half": chars(0x1F1F5, 0x1F1F1, 0x1F1FA),
    "a joiner between letters": chars(0x61, ZWJ, 0x62),
    "a joiner twice": chars(0x1F4BB, ZWJ, ZWJ, 0x1F44D),
    "a mark with nothing before it": chars(ACUTE, 0x61, 0x62, 0x63),
    "a mark after a line break": chars(0x61, 0x0A, ACUTE, 0x62),
    "a mark after an invisible space": chars(0x200B, ACUTE),
}

#: Sequences Unicode joins and Herald does not, so Herald counts them high.
COUNTED_HIGH = {
    "hangul jamo": chars(0x1100, 0x1161, 0x11A8),
    "a devanagari conjunct": chars(0x0915, 0x094D, 0x0937, 0x093F),
    "a windows line break": chars(0x61, 0x0D, 0x0A, 0x62),
    "a thai vowel": chars(0x0E01, 0x0E33),
}

#: Letters, marks, joiners, controls, pictographs, flags and tags, for random strings.
ALPHABET = [
    *(0x61, 0x62, 0x20, 0x23, 0x0A, 0x0D),
    *(ACUTE, ZWJ, 0x200C, VS16, KEYCAP, 0x0600, 0x200B, 0x2065, 0x2028, 0x0E33, 0x0903),
    *(0x1100, 0x1161, 0x11A8, 0xAC00, 0x0915, 0x094D, 0x093F, 0x093E),
    *(0x2640, 0x26F9, 0x2764, 0x2194, 0x2B1B),
    *(0x1F44D, 0x1F3FF, 0x1F469, 0x1F4BB, 0x1F1F5, 0x1F1F1, 0x1F3F4, 0xE0067, 0xE007F),
    *(0x1F525, 0x1F600, 0x1F7E9, 0x1F308, 0x1FAE8, 0xE0100),
]


@pytest.mark.parametrize("text", SEQUENCES.values(), ids=SEQUENCES.keys())
def test_what_a_reader_sees_as_one_character_counts_as_one(text: str) -> None:
    assert graphemes(text) == unicode_count(text)


@pytest.mark.parametrize("text", COUNTED_HIGH.values(), ids=COUNTED_HIGH.keys())
def test_what_it_does_not_join_counts_high(text: str) -> None:
    assert graphemes(text) > unicode_count(text)


def test_it_never_counts_below_unicode() -> None:
    rng = random.Random(20261010)  # noqa: S311 - it picks test strings, not secrets
    for _ in range(50_000):
        text = chars(*(rng.choice(ALPHABET) for _ in range(rng.randint(1, 12))))
        assert graphemes(text) >= unicode_count(text), [hex(ord(char)) for char in text]


def test_clusters_cut_the_text_wherever_unicode_does() -> None:
    """The pieces make the text again, as many as graphemes counts, and each lies within one of
    Unicode's: where it cannot tell, it cuts."""
    rng = random.Random(6863)  # noqa: S311 - it picks test strings, not secrets
    for _ in range(20_000):
        text = chars(*(rng.choice(ALPHABET) for _ in range(rng.randint(1, 12))))
        pieces = clusters(text)
        assert "".join(pieces) == text
        assert len(pieces) == graphemes(text)
        unicode_cuts = set(itertools.accumulate(map(len, CLUSTER.findall(text))))
        assert unicode_cuts <= set(itertools.accumulate(map(len, pieces))), [hex(ord(char)) for char in text]


def test_every_pictograph_it_joins_is_one_to_unicode() -> None:
    for first, last in _PICTOGRAPHS:
        for code in range(first, last + 1):
            assert PICTOGRAPH.fullmatch(chr(code)), hex(code)


def test_every_character_it_joins_to_the_one_before_is_joined_by_unicode() -> None:
    joined = [code for code in range(0x110000) if not 0xD800 <= code <= 0xDFFF and _extends(chr(code))]
    assert len(joined) > 1000, "the sweep found the marks it exists to check"
    assert [hex(code) for code in joined if unicode_count("a" + chr(code)) != 1] == []


@pytest.mark.parametrize(
    ("text", "links"),
    [
        ("see https://example.com/a.", ["https://example.com/a"]),
        ("(https://example.com/x)", ["https://example.com/x"]),
        (
            "https://example.com/ and HTTP://EXAMPLE.ORG/b?q=1, then",
            ["https://example.com/", "HTTP://EXAMPLE.ORG/b?q=1"],
        ),
        ("no link at example.com", []),
    ],
)
def test_a_link_ends_before_the_punctuation_after_it(text: str, links: list[str]) -> None:
    assert LINK.findall(text) == links


def test_links_weigh_what_the_platform_says() -> None:
    text = "Out now: https://example.com/a/very/long/path/to/the/release/notes."
    assert len(with_links_as(text, 23)) == len("Out now: ") + 23 + len(".")
