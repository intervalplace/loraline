"""What somebody looks like.

A nick is identity, a colour is identity, and so is a face. It lived in one
application for a while, which meant you had a face there and were a coloured
dot everywhere else, and that is a seam in a thing that is otherwise one
program on one radio.

So the picture is stored once, beside the keypair, and handed out the way a
key is: on meeting, to people you can actually hear. Anything built on top can
ask for it rather than inventing its own.

Thirty-two pixels, eight colours, and the eight colours travel with it. Twelve
square made a face a blot and twenty-four lost the eyes: at that size a pupil
is one pixel and eight colours cannot spare one for it. Thirty-two is where a
photograph still looks like the person.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

SIDE = 32
DEPTH = 3                 # three bits a pixel, so eight colours
PALETTE_BYTES = 16        # eight colours at four bits a channel

# What a picture uses if it names none. Chosen to sit together rather than to
# cover the spectrum: a palette somebody can draw a passable face out of beats
# a wider one they cannot.
#
# Every channel is a multiple of seventeen, because the packed form keeps four
# bits each and nothing else survives the round trip. The paper used to be
# #f4efe4 and came back as #ffeeee, which is pink, so every creature sat on a
# pink square.
DEFAULT_PALETTE = ("#111111", "#eeeedd", "#cc5544", "#ddaa33",
                   "#669944", "#337799", "#7755aa", "#888877")


def _pack_palette(colours) -> bytes:
    out = bytearray()
    for i in range(8):
        hexed = (colours[i] if i < len(colours) else "#000000").lstrip("#")
        r, g, b = (int(hexed[j:j + 2], 16) >> 4 for j in (0, 2, 4))
        out.append((r << 4) | g)
        out.append(b << 4)
    return bytes(out)


def _unpack_palette(raw: bytes) -> tuple:
    out = []
    for i in range(8):
        if len(raw) < i * 2 + 2:
            out.append("#000000")
            continue
        first, second = raw[i * 2], raw[i * 2 + 1]
        r, g, b = first >> 4, first & 0xF, second >> 4
        out.append("#%02x%02x%02x" % (r * 17, g * 17, b * 17))
    return tuple(out)


def pack(pixels, colours=None) -> str:
    """A picture into text: its palette, then three bits a pixel, then base64."""
    bits, value = 0, 0
    out = bytearray(_pack_palette(colours or DEFAULT_PALETTE))
    for pixel in list(pixels)[:SIDE * SIDE]:
        value = (value << DEPTH) | (int(pixel) & 0b111)
        bits += DEPTH
        while bits >= 8:
            bits -= 8
            out.append((value >> bits) & 0xFF)
    if bits:
        out.append((value << (8 - bits)) & 0xFF)
    return base64.b64encode(bytes(out)).decode("ascii")


def unpack(text: str) -> list:
    """And back. Anything malformed comes out blank rather than raising: this
    arrives from other people."""
    try:
        raw = base64.b64decode(text, validate=True)[PALETTE_BYTES:]
    except Exception:
        return [0] * (SIDE * SIDE)
    pixels, value, bits = [], 0, 0
    for byte in raw:
        value = (value << 8) | byte
        bits += 8
        while bits >= DEPTH and len(pixels) < SIDE * SIDE:
            bits -= DEPTH
            pixels.append((value >> bits) & 0b111)
    while len(pixels) < SIDE * SIDE:
        pixels.append(0)
    return pixels


def colours_of(text: str) -> tuple:
    try:
        return _unpack_palette(base64.b64decode(text, validate=True)[:PALETTE_BYTES])
    except Exception:
        return DEFAULT_PALETTE


def mark(picture: str) -> str:
    """Six hex characters, so two people can tell whether they have the same
    picture without either of them sending one. It rides on a heartbeat."""
    if not picture:
        return ""
    return hashlib.blake2b(picture.encode("ascii"), digest_size=3).hexdigest()


# The faces somebody has before they draw one.
#
# A hashed blotch is unique and says nothing. These are the MSN habit: a small
# set of things, and you are one of them until you decide otherwise. Somebody
# is the duck, and next week you still know who the duck is.
#
# Drawn at thirty-two in the eight default colours, with a hard edge so they
# read on paper and in the dungeon alike, then packed once and kept as text.
CREATURES = (
    # dog
    "ERDu0MVA2jBpQDeQdaCIcCSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSQAASSSSSSSSSSSSQG22ASSSSSSSSACSG2222CSACSSSQkgQ22222wQkgSSSEkkG222222EkkCSQkkkm222222kkkgSQkkkm2A2wG2kkkgSQkkkmwAGAA2kkkgSEkkkkwAGAA0kkkkCEkkkkwAGAA0kkkkCEkkkk2A2wG0kkkkCEkkkk222220kkkkCEkkkk222220kkkkCEkkkk2yAC20kkkkCEkkkk2QAAW0kkkkCQkkkgCQAASAkkkgSQkkkgSQAASQkkkgSQkkkgSSACSQkkkgSSEkkASSSSSQEkkCSSQkgSCSSSSCQkgSSSSACSQSSSQSSACSSSSSSSSCSSCSSSSSSSSSSSSQAASSSSSSSSSSSSSSSSSSSSSSQ==",
    # duck
    "ERDu0MVA2jBpQDeQdaCIcCSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSQAACSSSSSSSSSSSSCSSQSSSSSSSSSSSASSSSASSSSSSSSSQSSSSSSCSSSSSSSSQSSSSSSCSSSSSSSSCSSSSSSQSSSSSSSQSSSQCSSSCSSSSSSQSSSAASWSCSSSSSSQSSSAASW2CSSSSSSQSSSQCSW2wCSSSSSASSSSSSW22ySSSQASSSSSSSW22CSSSCSSSSSSSSW2ASSSQSSSSSSSSSWwSSSSCSSSSSSSSSWCSSSQSSSSSSSSSSASSSSCSSSSSSSSSSCSSSSCSSSSSSSSSSCSSSSCSSSSSSSSSSCSSSSCSSSSSSSSSSCSSSSCSSSSSSSSSSCSSSSQSSSSSSSSSQSSSSSSCSSSSSSSSCSSSSSSQSSSSSSSQSSSSSSSSCSSSSSSCSSSSSSSSG222yQASSSSSSSSSQG2wACSSSSSSSSSSSQwCSSSSSSSSQ==",
    # cat
    "ERDu0MVA2jBpQDeQdaCIcCSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSCSSSSSSCSSSSSSSRwSSSSSRwSSSSSSSR+CSSSSPwSSSSSSSR/wSSSR/wSSSSSSSP/+CSSP/+CSSSSSSP/+AAAP/+CSSSSSSP///////+CSSSSSSP///////+CSSSSSSP///////+CSSSSSR/////////wSSSSSR/////////wSSSSSR/////////wSSSSSR/////////wSSSSSP//5P//J//+CSSSSP//JJ/5JP/+CSSSSP//JJ/5JP/+CSSSSP//JJ/5JP/+CSSSSP//JJ/5JP/+CSSSSP//5P//J//+CSSSSQAB//1//wAASSSSAB///+kv///wACSSSSP///1///+CSSSSSSR///////wSSSSSSSSP/////+CSSSSSSSSR/////wSSSSSSSSSSB///wCSSSSSSSSSSSAAACSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSQ==",
    # fish
    "ERDu0MVA2jBpQDeQdaCIcCSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSQSSSSSSSSSSSSSSSKCSSSSSSSSSSSSSRaCSSSSSSSSSSSSSLbQSSSSSSSSSSSSSLbQSSSSSSSSSSSSRbbQSSSSSSSSSSSQLbbaCSSSSQSSSSQLbbbaCSSSSKCSSQLbbbbbQCSSRaCSSLbbbbbbbQSSLaCSRbbbbbbbbaCRbaCSLbSTbbbbbbQLbaCRbaSCbbbbbbbbbaCRbaQAbbbbbbbbbaCRbaSCbbbbbbbbbaCRbbSTbbbbbbbbbaCRbbbbbbbbbbbbbaCSLbbbbbbbbbQLbaCSRbbbbbbbbaCRbaCSSLbbbbbbbQSSLaCSSQLbbbbbQCSSRaCSSSQLbbbQCSSSSKCSSSSQAAACSSSSSQSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSQ==",
    # owl
    "ERDu0MVA2jBpQDeQdaCIcCSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSCSSSSSSCSSSSSSSRgSSSSSRgSSSSSSSRsCSSSSNgSSSSSSSNsCAAACNsCSSSSSSNthttthtsCSSSSSRtttttttttgSSSSSRtttttttttgSSSSSNtttttttttsCSSSSMNtttttttsMCSSSSRtttttttttgSSSSSNtiSdtsSTtsCSSSSNsSSTtiSSdsCSSSSNiSAScSQCTsCSSSRtiQACcSAATtgSSSRtiQACcSAATtgSSSRtiSAScSQCTtgSSSRtsSSTtiSSdtgSSSRttiSc28STttgSSSRtttts29ttttgSSSSNttttnttttsCSSSSNttttnttttsCSSSSNttttnttttsCSSSSRtttttttttgSSSSSSNtttttttsCSSSSSSRtttttttgSSSSSSSSNtttttsCSSSSSSSSRtttttgSSSSSSSSSSBtttgCSSSSSSSSSSSAAACSSSSSSSSSSSSSSSSSSSSSQ==",
    # frog
    "ERDu0MVA2jBpQDeQdaCIcCSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSACSSSSACSSSSSSSRJASSSRJASSSSSSSJJICSSJJICSSSSSRISZASRISZASSSSSJCQTICJCQTICSSSSJCADICJCADICSSSSJCADIAJCADICSSSSJCQTJJJCQTICSSSSRISZJJJISZASSSSSSJJJJJJJJICSSSSSRJJJJJJJJJASSSSSJJJJJJJJJJICSSSRJJJJJJJJJJJASSSRJJJJJJJJJJJASSSJJJJJJJJJJJJICSSJJJJJJJJJJJJICSSJJJJJJJJJJJJICSSJJJJJJJJJJJJICSSRJJJBJJJBJJJASSSRJJJAJJIBJJJASSSSJJJIAAAJJJICSSSSRJJJAABJJJASSSSSSJJJJJJJJICSSSSSSQJJJJJJIASSSSSSSSQBJJJAASSSSSSSSSSSAAACSSSSSSSSSSSSSSSSSSSSSQ==",
    # crab
    "ERDu0MVA2jBpQDeQdaCIcCSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSACSSSSSSSSSQACSAkgCSSSSSSSSEkgQkkkgSSSSSSSQkkkAkkkgSSSSSSSQkkkEkkkkCSSSSSSEkkkkkkkkCSSSSSSEkkkkkkkkCSSSSSSEkkkgkkkgSSSSSSSQkkkAkkkgSQAAAASQkkkCAkgEAEkkkkAEkkgSSAAkkkkkkkkkAACSSSQEkkkkkkkkASSSSSEkkgEkkAkkkCSSSSEkkAAkgAEkkCSSSQkkkAAkgAEkkgSSSQkkkgEkkAkkkgSSSQkkkkkkkkkkkgSSSSEkkkkkkkkkkCSSSSEkkkkkkkkkkCSSSSQEkkkkkkkkASSSSSSQkkkkkkkgSSSSSSSQkEkkkkAkCSSSSSSQkAAEgAQkCSSSSSSEgSQkCSEgSSSSSSSEgSQkCSEgSSSSSSQkCSEgSQkCSSSSSSQkCSEgSQkCSSSSSSSASSQCSSASSSSQ==",
    # snail
    "ERDu0MVA2jBpQDeQdaCIcCSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSAAASSSSSSSSSQCSAkkkASSSSSASSAAQkkkkkCSSSQACSAAEkkkkkgSSSQACQwAkkkkkkkCSSGASQ2EkkkkkkkgSSGwSQ2kkkm20kkkCSQwSQ2kkk222kkkCSQ2CQ2kkm2220kkgSQ2CG0kk22k22kkgSQ2CG0kk20km2kkgSSGCG0kk20km2kkgSSGwG0kk22k22kkgSSGw20kkm2220kkgSSQ222kkk222kkkCSSG222kkkm20kkkCSQ22220kkkkkkkgSSG22222kkkkkkkCSSG222220kkkkkgSSSQ222222kkkkkCSSSSG222222kkkASSSSSQ222222wAASSSSSSSA2222wCSSSSSSSSSSAAAACSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSQ==",
    # bee
    "ERDu0MVA2jBpQDeQdaCIcCSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSAACSSSQAASSSSSSASSQCSQCSSASSSSQSSSSQSCSSSSCSSSCSSSSSASSSSSQSSSCSSSSSASSSSSQSSQSSSSSSSSSSSSSCSQSSSSSSSSSSSSSCSQSSSSSSSSSSSSSCSSCSSSSW22ySSSQSSSCSSSwA2AGwASQSSSQSSWwA2AGwASCSSSSAS2wA2AGwAASSSSSSGAwA2AGwAwSSSSSSAAAA2AGwAwSSSSSQwAAA2AGwA2CSSSSQ2AwA2AGwA2CSSSSQ22wA2AGwA2CSSSSQ22wA2AGwA2CSSSSSG2wA2AGwAwSSSSSSG2wA2AGwAwSSSSSSQ2wA2AGwACSSSSSSSGwA2AGwASSSSSSSSQwA2AGwASSSSSSSSSCG22wCSSSSSSSSSSSQAACSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSQ==",
    # mouse
    "ERDu0MVA2jBpQDeQdaCIcCSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSAACSSSSSSAACSSSR//wSSSSSR//wSSSP//+CSSSSP//+CSR////wSSSR////wSP/0l/+CSSP/0l/+CP+kkv+AAAP+kkv+CP+kkv+P/+P+kkv+CP+kkv/////+kkv+CP/0l///////0l/+CR/////////////wSSP///////////+CSSR///+B/wP///wSSSSAP/wAOAB/+ACSSSSSP/wAOAB/+CSSSSSSP/+B/wP/+CSSSSSSP//0kl//+CSSSSSSR//+kv//wSSSSSSSR//+kv//wSSSSSSSSP//1//+CSSSSSSSSQP///+ASSSSSSSSSSQP/+ASSSSSSSSSSSSQAASSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSQ==",
)

CREATURE_NAMES = ("dog", "duck", "cat", "fish", "owl",
                  "frog", "crab", "snail", "bee", "mouse")


def identicon(address: str) -> str:
    """The face an address has before anybody sets one.

    Derived, so every machine works out the same one for the same person and
    nothing has to be sent.
    """
    seed = hashlib.blake2b(address.encode("utf-8"), digest_size=8).digest()
    return CREATURES[seed[0] % len(CREATURES)]


def creature_of(address: str) -> str:
    """Which one, by name. For saying "you are the duck" rather than showing
    a picture."""
    seed = hashlib.blake2b(address.encode("utf-8"), digest_size=8).digest()
    return CREATURE_NAMES[seed[0] % len(CREATURE_NAMES)]


def from_image(source, side: int = SIDE):
    """Any picture into something that fits on a radio.

    Square from the middle, high enough to catch a face, then eight colours
    chosen from the image itself. What comes out is roughly an icon from 1993,
    which is the honest result of asking a photograph across a link that
    carries a few hundred bytes at a time.

    Sharpen first and do not dither: dithering scatters a one pixel eye across
    three pixels of nothing, and at this size every feature of a face is one
    pixel.

    Needs Pillow, which nothing else here does, so it is imported inside.
    """
    from PIL import Image, ImageEnhance
    picture = source if hasattr(source, "convert") else Image.open(source)
    picture = picture.convert("RGB")
    edge = min(picture.size)
    left = (picture.width - edge) // 2
    top = (picture.height - edge) // 3
    picture = picture.crop((left, top, left + edge, top + edge))
    picture = picture.resize((side, side), Image.LANCZOS)
    picture = ImageEnhance.Sharpness(picture).enhance(2.4)
    reduced = picture.quantize(colors=8, method=Image.MEDIANCUT, dither=Image.NONE)
    table = (reduced.getpalette() or [0] * 24)[:24]
    colours = tuple("#%02x%02x%02x" % tuple(table[i * 3:i * 3 + 3]) for i in range(8))
    return pack(list(reduced.tobytes()), colours)


# --------------------------------------------------------------------------
# Keeping it
# --------------------------------------------------------------------------

@dataclass
class Faces:
    """Yours, and everybody's you have been handed.

    Kept beside the identity, because that is what it belongs to. A face is
    set once and then almost never, so this is written when it changes and
    read at startup.
    """

    mine: str = ""
    theirs: dict = None
    path: object = None

    def __post_init__(self):
        if self.theirs is None:
            self.theirs = {}

    def load(self, path) -> "Faces":
        self.path = Path(path)
        if not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return self
        self.mine = raw.get("mine", "") or ""
        for address, picture in (raw.get("theirs") or {}).items():
            if isinstance(picture, str):
                self.theirs[address] = picture
        return self

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(
                {"mine": self.mine, "theirs": self.theirs},
                separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            pass

    def of(self, address: str, fallback: bool = True) -> str:
        """Somebody's face, or the one their address already had."""
        here = self.theirs.get(address)
        if here:
            return here
        return identicon(address) if fallback else ""

    def own(self, address: str) -> str:
        return self.mine or identicon(address)

    def set_mine(self, picture: str) -> None:
        self.mine = picture
        self.save()

    def learn(self, address: str, picture: str) -> bool:
        """Take somebody's face. Returns True if it was new."""
        if not picture or self.theirs.get(address) == picture:
            return False
        self.theirs[address] = picture
        self.save()
        return True


# --------------------------------------------------------------------------
# Handing it over
# --------------------------------------------------------------------------
#
# Seven hundred odd characters is five or six frames, so this cannot ride on a
# heartbeat the way a nick does. What rides on the heartbeat is a six
# character mark; anybody who has a different one asks, and the picture comes
# back in pieces.

APP = "lface"
CHUNK = 90               # characters a frame, comfortably inside one


def ask(mark_wanted: str) -> str:
    return "?" + mark_wanted


def offer(picture: str) -> list:
    """A picture, cut into frames."""
    pieces = [picture[i:i + CHUNK] for i in range(0, len(picture), CHUNK)]
    total = len(pieces)
    return [f"={mark(picture)}:{n}/{total}:{piece}"
            for n, piece in enumerate(pieces)]


@dataclass
class Arriving:
    """Pieces of somebody's face, until there are enough of them."""
    pieces: dict = None
    total: int = 0
    mark: str = ""

    def __post_init__(self):
        if self.pieces is None:
            self.pieces = {}

    def take(self, payload: str) -> str | None:
        """Feed one frame in. Returns the picture once it is whole.

        A piece whose mark is not the one being collected starts a fresh
        collection, so two faces arriving at once cannot be spliced into a
        third that belongs to nobody.
        """
        if not payload.startswith("="):
            return None
        try:
            stamp, index, piece = payload[1:].split(":", 2)
            n, total = (int(part) for part in index.split("/"))
        except Exception:
            return None
        if total < 1 or n < 0 or n >= total:
            return None
        if stamp != self.mark:
            self.pieces, self.mark, self.total = {}, stamp, total
        self.pieces[n] = piece
        if len(self.pieces) < self.total:
            return None
        whole = "".join(self.pieces[i] for i in sorted(self.pieces))
        if mark(whole) != self.mark:
            # Something arrived wrong. Throw it away rather than keep a face
            # that is not the one that was sent.
            self.pieces, self.mark, self.total = {}, "", 0
            return None
        self.pieces, self.mark, self.total = {}, "", 0
        return whole
