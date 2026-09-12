"""Everything that rides on loraline, in one process.

A serial port can only be opened once. That is not a detail, it is the whole
argument: two programs cannot share a radio, so running the chat and a game at
the same time was impossible, and quitting the chat to play a game that has
chat in it is plainly silly.

So loraline hosts. It owns the radio, the identity and the passphrase, and
anything else that wants the air registers a panel and is handed the frames
addressed to it. One setup, one budget, one conversation underneath all of it.

A panel is four methods and none of them are required. The protocol is small
on purpose: anybody should be able to put something on this radio without
reading much.
"""

from __future__ import annotations

import importlib
import time
from dataclasses import dataclass, field


class Panel:
    """Something riding on loraline.

    Subclass, set `tag` to the application name that goes on the wire, and
    implement whichever of these you need. The host calls them; nothing here
    owns a loop, a radio or a port.
    """

    #: What goes in the application field of a frame. Must be unique.
    tag = ""
    #: What the person sees in the switcher.
    title = ""
    #: Where its page lives, as a path.
    route = ""
    #: True if it should keep working while somebody is looking at something
    #: else. A game is not; a thing that holds messages for other people is.
    always = False

    def start(self, host: "Host") -> None:
        """Called once, when the radio is up."""

    def heard(self, src: str, payload: str) -> None:
        """A frame arrived with our tag on it."""

    def tick(self, now: float) -> None:
        """Called every time round the loop, about five times a second."""

    def handle(self, order: dict) -> None:
        """Somebody pressed something on our page."""

    def snapshot(self) -> dict:
        """What our page needs to draw. Kept small: it goes out on every
        change, to every open tab."""
        return {}

    def page(self) -> str:
        """Our page, as HTML."""
        return ""


# Panels that ship separately. Each is its own repository and none of them are
# required: if it is not installed, it is not in the switcher.
KNOWN = (
    ("catacomms", "catacomms.panel", "DelvePanel"),
    ("longshore", "longshore.panel", "ShorePanel"),
    ("hearsay", "hearsay.panel", "HearsayPanel"),
)


def discover() -> list:
    """Whatever is installed alongside.

    An import that fails is a panel that is not there, which is a normal state
    and not an error: most people will have one of these, not four.
    """
    found = []
    for _, where, what in KNOWN:
        try:
            module = importlib.import_module(where)
            found.append(getattr(module, what)())
        except Exception:
            continue
    return found


@dataclass
class Host:
    """What a panel is given: the radio, and a way to be heard."""

    client: object
    identity: object
    panels: list = field(default_factory=list)
    _note: object = None

    def send(self, tag: str, payload: str) -> None:
        self.client.session.send_app(tag, payload)

    def affordable(self, tag: str, payload: str) -> bool:
        """Would this fit in what is left of the hour?

        Every panel shares one budget, because every panel shares one radio.
        A delve in progress and a node handing on posts are spending the same
        thirty-six seconds, and a panel that does not ask will find its frames
        quietly dropped: nothing retransmits application frames.
        """
        from . import protocol as proto
        from .crypto import GROUP
        frame = proto.data(self.client.session.address, GROUP, tag, payload)
        return self.client.link.can_send(frame, GROUP)

    def note(self, text: str, role: str = "muted") -> None:
        if self._note is not None:
            self._note(text, role)

    @property
    def address(self) -> str:
        return self.client.session.address

    @property
    def nick(self) -> str:
        return self.client.session.nick

    def peers(self) -> list:
        return self.client.session.online_peers()


# The switcher, put into every page by the host rather than copied into four
# of them. It is deliberately plain: each application has its own look and a
# bar that tried to match all of them would match none.
NAV_STYLE = """
<style id="loraline-nav">
#loraline-nav{position:sticky;top:0;z-index:99;display:flex;gap:.1rem;
  align-items:center;padding:.35rem .6rem;background:#1b1a18;color:#cfc9bf;
  font:13px ui-monospace,Menlo,Consolas,monospace;border-bottom:1px solid #000}
#loraline-nav a{color:#cfc9bf;text-decoration:none;padding:.25rem .6rem;
  border-radius:3px}
#loraline-nav a:hover{background:#2c2a26;color:#fff}
#loraline-nav a[aria-current]{background:#b01b62;color:#fff}
#loraline-nav .mark{color:#ea8fb4;margin-right:.5rem;letter-spacing:-1px}
#loraline-nav .on{margin-left:auto;color:#7f7a72;font-size:11px}
</style>
"""


def nav_html(panels, here: str) -> str:
    links = [('<a href="/"%s>chat</a>'
              % (' aria-current="page"' if here in ("", "/") else ""))]
    for panel in panels:
        current = ' aria-current="page"' if here.rstrip("/") == panel.route.rstrip("/") else ""
        links.append(f'<a href="{panel.route}"{current}>{panel.title}</a>')
    running = [p.title for p in panels if p.always]
    tail = (f'<span class="on">{", ".join(running)} running</span>'
            if running else "")
    return (NAV_STYLE + '<div id="loraline-nav"><span class="mark">'
            '&#x2571;&#x2571;&#x2572;</span>' + "".join(links) + tail + "</div>")
