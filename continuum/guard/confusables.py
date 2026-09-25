# ruff: noqa: RUF001
"""Look-alike letters (Cyrillic, Greek, a few others) an attacker can substitute for Latin
letters to slip a forbidden word past substring matching. The characters in this table are
*deliberately* the ambiguous ones, hence the lint exemption for this one data file.
"""

from __future__ import annotations

CONFUSABLES = str.maketrans(
    {
        "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i",
        "ѕ": "s", "ј": "j", "ԁ": "d", "ɡ": "g", "һ": "h", "ո": "n", "ս": "u",
        "α": "a", "ε": "e", "ο": "o", "ρ": "p", "ν": "v", "ι": "i", "τ": "t", "υ": "u",
    }
)  # fmt: skip
