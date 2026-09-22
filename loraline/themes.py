"""Themes.

Six colours and everything else follows. The chat and hearsay both draw on
paper, so they share this table rather than each keeping their own idea of
what the paper looks like.

What a theme does not touch, and should not:

  - the creature faces, whose eight colours are in the wire format itself. Two
    people who are the same creature have the same picture, and a theme that
    changed it would be a theme that changed what other people see.
  - longshore's coast, which is a fixed pixel palette.
  - catacomms, which is a dark room on purpose.

So a theme is the paper and the ink, not the places. The creatures were drawn
with hard outlines so they sit on either.
"""
from __future__ import annotations

# name -> (paper, panel, ink, soft, faint, rule, mark)
#
# mark is the one accent: links, your own name, the things you can press. One
# accent rather than a palette, because two would need a rule about which goes
# where and there isn't one.
THEMES: dict = {
    # The identity: warm off-white and magenta.
    "paper": ("#f7f5f0", "#eeebe3", "#22201d", "#5d5952", "#8d8880",
              "#dcd7cc", "#b01b62"),
    # Neutral slate and warm amber, so the dark one is not paper inverted.
    "night": ("#15171a", "#1e2125", "#e7e4de", "#a9a59d", "#7a766f",
              "#2c3036", "#e3a857"),
    # Actually pink, with a plum accent. It used to be off-white with a pink
    # thought and another magenta, three steps from paper on a scale where
    # ten is hard to tell apart.
    "rose":  ("#f1c9d6", "#eab9c9", "#2b1b21", "#65434f", "#8c6873",
              "#ddafbf", "#5e2a6e"),
    # Pale sea and deep teal.
    "sea":   ("#eef5f4", "#dfecea", "#1a2524", "#4d605d", "#7c8f8c",
              "#cfe0dd", "#10766e"),
    # Plainly violet with coral, so it cannot be mistaken for night, which it
    # used to be: both near-black, both with a pink accent.
    "dusk":  ("#271d3a", "#322748", "#ece6f5", "#b3a8c8", "#8a7fa3",
              "#3d3257", "#f29478"),
}

FIELDS = ("paper", "panel", "ink", "soft", "faint", "rule", "mark")

#: Which are dark, so a page can pick a colour-scheme hint that matches and
#: the scrollbars and form controls come out right rather than white-on-dark.
DARK = frozenset({"night", "dusk"})

DEFAULT = "paper"


def names() -> tuple:
    return tuple(THEMES)


def pick(name: str) -> str:
    """The theme by that name, or the default. Never raises: a settings file
    naming a theme this build has never heard of should open, not fail."""
    return name if name in THEMES else DEFAULT


def variables(name: str) -> str:
    """The CSS custom properties for a theme, ready to drop into :root."""
    chosen = THEMES[pick(name)]
    out = ";".join(f"--{field}:{value}"
                   for field, value in zip(FIELDS, chosen))
    # hearsay calls its accent --red, from when it was one. Both names are set
    # so neither page has to care which it grew up with.
    return out + f";--red:{chosen[FIELDS.index('mark')]}"


def scheme(name: str) -> str:
    """light or dark, for the browser's own furniture."""
    return "dark" if pick(name) in DARK else "light"
