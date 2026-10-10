"""Counting text the way platforms count it, with the standard library alone.

Platforms disagree about what a character is. Bluesky counts graphemes (what a reader sees as
one character), Mastodon and X count every link as 23 characters whatever its length, and most
of the rest count code points. These helpers give a channel those pieces to build its own
``count`` from.

:func:`graphemes` approximates Unicode's extended grapheme clusters without the tables Python
does not ship. It joins the sequences an announcement carries: combining marks, variation
selectors, skin tones, keycaps, flag pairs, tag flags and emoji joined by a zero-width joiner.
Where it differs from Unicode it counts high, never low, so a post it lets through is never one
the platform refuses for its length. The tests hold it to that against a full implementation.
"""

from __future__ import annotations

import re
import unicodedata

#: A link as the platforms that weigh links find it: a scheme, then everything to the next space,
#: less any punctuation it ends on. That punctuation then counts as text, so where a platform
#: takes it into the link the count comes out high rather than low.
LINK = re.compile(r"https?://\S*[^\s.,:;!?'\")\]}>]", re.IGNORECASE)

_ZWJ = "\N{ZERO WIDTH JOINER}"
_ZWNJ = "\N{ZERO WIDTH NON-JOINER}"
_REGIONAL_INDICATORS = range(0x1F1E6, 0x1F200)
_SKIN_TONES = range(0x1F3FB, 0x1F400)
_TAGS = range(0xE0020, 0xE0080)
#: Unassigned code points Unicode treats as controls: its reserved default-ignorable ranges.
_IGNORABLE_UNASSIGNED = ((0x2065, 0x2065), (0xFFF0, 0xFFF8), (0xE0000, 0xE0FFF))

#: Part of Unicode's Extended_Pictographic: the emoji blocks, and the symbols that emoji ZWJ
#: sequences join. Only these join across a zero-width joiner here. A pictograph missing from
#: this list makes its sequence count high; a character in it that is not a pictograph could
#: make one count low, so nothing goes in that Unicode does not list.
_PICTOGRAPHS = (
    (0x2194, 0x2199),  # arrows: head shaking
    (0x2620, 0x2620),  # skull and crossbones: pirate flag
    (0x2640, 0x2640),  # female sign
    (0x2642, 0x2642),  # male sign
    (0x2695, 0x2696),  # staff of Aesculapius, scales
    (0x26A7, 0x26A7),  # transgender symbol
    (0x26D3, 0x26D3),  # chains: broken chain
    (0x26F7, 0x26FA),  # skier to tent: person bouncing a ball
    (0x2708, 0x2708),  # airplane
    (0x2744, 0x2744),  # snowflake
    (0x2764, 0x2764),  # heart: heart on fire
    (0x27A1, 0x27A1),  # rightwards arrow: walking to the right
    (0x2B1B, 0x2B1C),  # large squares: black cat
    (0x1F300, 0x1F321),  # weather and sky: rainbow flag
    (0x1F324, 0x1F393),
    (0x1F39E, 0x1F3F0),
    (0x1F3F3, 0x1F3F5),  # flags
    (0x1F3F7, 0x1F3FA),
    (0x1F400, 0x1F4FD),
    (0x1F4FF, 0x1F53D),
    (0x1F573, 0x1F57A),
    (0x1F5E8, 0x1F5E8),  # speech bubble: eye in a speech bubble
    (0x1F5FA, 0x1F64F),
    (0x1F680, 0x1F6C5),
    (0x1F6CB, 0x1F6D2),
    (0x1F6D5, 0x1F6E5),
    (0x1F6EB, 0x1F6F0),
    (0x1F6F3, 0x1F6FF),
    (0x1F7E0, 0x1F7EB),  # coloured circles and squares: lime
    (0x1F90C, 0x1F93A),
    (0x1F93C, 0x1F945),
    (0x1F947, 0x1F9FF),
    (0x1FA70, 0x1FAFF),
)

# Where a character stands in an emoji ZWJ sequence, which Unicode's rule GB11 defines as a
# pictograph, any extenders, one joiner, then the pictograph it joins.
_OUTSIDE, _AFTER_PICTOGRAPH, _AFTER_JOINER = 0, 1, 2


def _pictograph(code: int) -> bool:
    return any(first <= code <= last for first, last in _PICTOGRAPHS)


def _extends(char: str) -> bool:
    """Whether ``char`` belongs to the character before it rather than starting one."""
    code = ord(char)
    return (
        unicodedata.combining(char) != 0
        or unicodedata.category(char) in ("Mn", "Me")
        or code in _SKIN_TONES
        or code in _TAGS
        or char in (_ZWJ, _ZWNJ)
    )


def _control(char: str) -> bool:
    """Whether nothing may join ``char``: a control, a separator or an invisible format character."""
    category = unicodedata.category(char)
    if category in ("Cc", "Cs", "Zl", "Zp"):
        return True
    if category == "Cf":
        return not _extends(char)
    code = ord(char)
    return category == "Cn" and any(first <= code <= last for first, last in _IGNORABLE_UNASSIGNED)


def clusters(text: str) -> list[str]:
    """``text`` cut into the characters a reader sees, as :func:`graphemes` counts them.

    Where it cannot tell, it cuts, so a cluster here is never more than Unicode joins.
    """
    found: list[str] = []
    previous = ""
    sequence = _OUTSIDE
    lone_flag = False  # the current character is one regional indicator, open to its pair
    for char in text:
        code = ord(char)
        if previous and not _control(previous) and _extends(char):
            if char == _ZWJ:
                sequence = _AFTER_JOINER if sequence == _AFTER_PICTOGRAPH else _OUTSIDE
            elif sequence != _AFTER_PICTOGRAPH:
                sequence = _OUTSIDE
            lone_flag = False
            found[-1] += char
        elif sequence == _AFTER_JOINER and _pictograph(code):
            sequence = _AFTER_PICTOGRAPH
            found[-1] += char
        elif lone_flag and code in _REGIONAL_INDICATORS:
            lone_flag = False
            sequence = _OUTSIDE
            found[-1] += char
        else:
            found.append(char)
            sequence = _AFTER_PICTOGRAPH if _pictograph(code) else _OUTSIDE
            lone_flag = code in _REGIONAL_INDICATORS
        previous = char
    return found


def graphemes(text: str) -> int:
    """How many characters a reader sees in ``text``. An approximation that only ever counts high."""
    return len(clusters(text))


def utf16_length(text: str) -> int:
    """How long ``text`` is in UTF-16 code units, as JavaScript and Java count it.

    A character outside the Basic Multilingual Plane, most emoji among them, counts two.
    """
    return len(text.encode("utf-16-le")) // 2


def with_links_as(text: str, weight: int) -> str:
    """``text`` with every link replaced by ``weight`` placeholder characters, for counting."""
    return LINK.sub("x" * weight, text)


__all__ = ["LINK", "clusters", "graphemes", "utf16_length", "with_links_as"]
