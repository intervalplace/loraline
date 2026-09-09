"""Terminal interface on the standard library's curses module.

No UI framework dependency, so the whole client is Python plus a serial port.

The window model is MSN's: one contact list, one conversation open at a time,
Tab to cycle, unread counts on the ones you aren't looking at.
"""

from __future__ import annotations

import curses
import locale
import time

from . import protocol as proto
from .client import Client
from .crypto import GROUP
from .protocol import Delivery, Status
from .session import (
    PALETTE,
    DeliveryEvent,
    MessageEvent,
    SystemEvent,
)

SIDEBAR_W = 24
MIN_SIDEBAR_COLS = 62
MAX_HISTORY = 400

EMOTICONS = {
    ":)": "\u263a", ":-)": "\u263a", ":(": "\u2639", ":-(": "\u2639",
    ";)": "\u263b", ":D": "\u263a", ":P": "\u203c", "<3": "\u2665",
    "(Y)": "\u2714", "(N)": "\u2718",
}

# The site's chart palette, mapped onto xterm-256 indices. Muted on purpose:
# a chat client sits in a terminal for hours, and saturated primaries wear
# badly. Falls back to the eight basic colours where 256 are unavailable.
XTERM = {
    "person": [72, 67, 175, 179, 108, 174, 146, 109],
    "accent": 168, "muted": 244, "rule": 240, "warn": 179,
}
BASIC = {
    "person": [curses.COLOR_GREEN, curses.COLOR_CYAN, curses.COLOR_MAGENTA,
               curses.COLOR_YELLOW, curses.COLOR_BLUE, curses.COLOR_RED,
               curses.COLOR_WHITE, curses.COLOR_CYAN],
    "accent": curses.COLOR_MAGENTA, "muted": curses.COLOR_WHITE,
    "rule": curses.COLOR_WHITE, "warn": curses.COLOR_YELLOW,
}
ROLES = ("accent", "muted", "rule", "warn")

HELP = [
    "Tab moves between conversations. /g goes to the group, "
    "/w dave opens a private chat with dave.",
    "/away /busy /brb /back change what others see next to your name. "
    "/psm sets the line underneath it.",
    "/whois dave shows their fingerprint, so you can check out loud that they "
    "are who you think.",
    "/status shows the radio settings. /colour 0-7 picks your colour. "
    "Ctrl+Q or /quit leaves. PgUp scrolls back.",
]


def unicode_ok() -> bool:
    encoding = (locale.getpreferredencoding(False) or "").lower()
    return "utf" in encoding


def logo() -> str:
    """The mark, in box-drawing diagonals: two up-chirps and a down-chirp.

    Falls back to ASCII where the terminal is not on a UTF-8 locale, which is
    the closest thing to a reliable test for whether those glyphs will draw.
    """
    return "\u2571\u2571\u2572" if unicode_ok() else "//\\"


def emote(text: str) -> str:
    for k, v in EMOTICONS.items():
        text = text.replace(k, v)
    return text


class Entry:
    __slots__ = ("kind", "who", "text", "colour", "seq", "when", "level")

    def __init__(self, kind, who="", text="", colour=0, seq=None, when=0.0, level="info"):
        self.kind, self.who, self.text = kind, who, text
        self.colour, self.seq, self.when, self.level = colour, seq, when, level


class CursesUI:
    def __init__(self, client: Client) -> None:
        self.client = client
        self.session = client.session
        self.log: dict[str, list[Entry]] = {GROUP: []}
        self.unread: dict[str, int] = {}
        self.active = GROUP
        self.buffer = ""
        self.cursor = 0
        self.scroll = 0
        self.running = True

    # -- conversations -----------------------------------------------------

    def order(self) -> list[str]:
        peers = sorted(self.session.conversations)
        peers = [k for k in peers if k != GROUP]
        peers.sort(key=lambda k: self.session.conversations[k].title.lower())
        return [GROUP] + peers

    def title_of(self, key: str) -> str:
        return "everyone" if key == GROUP else self.session.conversations[key].title

    def entries(self, key: str) -> list[Entry]:
        return self.log.setdefault(key, [])

    def switch(self, key: str) -> None:
        self.active = key
        self.unread[key] = 0
        self.scroll = 0

    def cycle(self, step: int) -> None:
        keys = self.order()
        if self.active not in keys:
            self.switch(GROUP)
            return
        self.switch(keys[(keys.index(self.active) + step) % len(keys)])

    # -- events ------------------------------------------------------------

    def brand(self, text: str) -> None:
        """One line at startup where the program names itself. Nowhere else:
        a mark parked permanently in the sidebar would report nothing true."""
        self.entries(GROUP).append(Entry("brand", text=text, when=time.time()))

    def system(self, text: str, level: str = "info", convo: str | None = None) -> None:
        target = convo if convo is not None else self.active
        entries = self.entries(target)
        entries.append(Entry("system", text=text, level=level, when=time.time()))
        del entries[:-MAX_HISTORY]

    def absorb(self, events) -> None:
        for event in events:
            if isinstance(event, MessageEvent):
                entries = self.entries(event.convo)
                entries.append(Entry(
                    "msg", who=event.who, text=emote(event.text), colour=event.colour,
                    seq=event.seq if not event.incoming else None, when=event.when,
                ))
                del entries[:-MAX_HISTORY]
                if event.incoming:
                    curses.beep()
                    if event.convo != self.active:
                        self.unread[event.convo] = self.unread.get(event.convo, 0) + 1
            elif isinstance(event, SystemEvent):
                self.system(event.text, event.level, event.convo)
            elif isinstance(event, DeliveryEvent):
                pass  # glyphs are read live from the session at draw time

    # -- rendering ---------------------------------------------------------

    def render_lines(self, width: int) -> list[list[tuple[str, object]]]:
        """History as display lines, each a list of (text, role) segments.

        Segments rather than one colour per line, so a timestamp can be quiet
        while the name carries the person's colour and the delivery mark
        carries the accent.
        """
        out: list[list[tuple[str, object]]] = []
        for entry in self.entries(self.active):
            if entry.kind == "brand":
                out.append([(logo() + "  ", "accent"), (entry.text, "muted")])
                continue
            if entry.kind == "system":
                role = "warn" if entry.level == "warn" else "muted"
                lines = self._wrap(entry.text, max(8, width - 2))
                out.append([("* ", "muted"), (lines[0], role)])
                out += [[("  ", "muted"), (line, role)] for line in lines[1:]]
                continue

            stamp = time.strftime("%H:%M", time.localtime(entry.when))
            name = f"{entry.who}: "
            mark = ""
            if entry.seq is not None:
                item = self.session.outgoing(entry.seq)
                if item is not None:
                    mark = " " + item.resolve().glyph + item.tally()

            lead = len(stamp) + 1 + len(name)
            body = self._wrap(entry.text, max(8, width - lead - len(mark)))
            out.append([(stamp + " ", "muted"), (name, entry.colour),
                        (body[0], "text")])
            indent = " " * min(lead, max(0, width // 3))
            out += [[(indent, "muted"), (line, "text")] for line in body[1:]]
            if mark:
                out[-1].append((mark, "accent"))
        return out

    @staticmethod
    def _wrap(text: str, width: int) -> list[str]:
        if width <= 1:
            return [text[:1]]
        lines, current = [], ""
        for word in text.split(" "):
            candidate = word if not current else current + " " + word
            if len(candidate) <= width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                while len(word) > width:
                    lines.append(word[:width])
                    word = word[width:]
                current = word
        lines.append(current)
        return lines or [""]

    def draw(self, stdscr) -> None:
        stdscr.erase()
        rows, cols = stdscr.getmaxyx()
        show_sidebar = cols >= MIN_SIDEBAR_COLS
        left = SIDEBAR_W + 1 if show_sidebar else 0
        main_w = max(10, cols - left - 1)   # addnstr clips the final column
        now = time.time()

        if show_sidebar:
            self._draw_sidebar(stdscr, rows, now)
            for y in range(rows - 1):
                self._put(stdscr, y, SIDEBAR_W, "\u2502", self._attr("rule"))

        title = self.title_of(self.active)
        suffix = ""
        if self.active != GROUP:
            peer = self.session.peers.get(self.active)
            if peer is not None:
                lock = "e2e" if self.session.keyring.knows(self.active) else "no key"
                suffix = f" [{peer.status.label}, {lock}]"
        self._put(stdscr, 0, left, "\u2500\u2500 ", self._attr("rule"))
        self._put(stdscr, 0, left + 3, title, self._attr("accent"))
        rest = f"{suffix} " + "\u2500" * main_w
        self._put(stdscr, 0, left + 3 + len(title), rest[:max(0, main_w - len(title) - 3)],
                  self._attr("rule"))

        convo_h = max(1, rows - 4)
        lines = self.render_lines(main_w)
        self.scroll = max(0, min(self.scroll, max(0, len(lines) - convo_h)))
        end = len(lines) - self.scroll
        for i, segments in enumerate(lines[max(0, end - convo_h):end]):
            x = left
            for text, role in segments:
                if x - left >= main_w:
                    break
                self._put(stdscr, i + 1, x, text[:main_w - (x - left)], self._attr(role))
                x += len(text)

        if self.scroll > 0:
            note = f"-- scrolled back {self.scroll} lines, End to return --"
            self._put(stdscr, convo_h, left, note[:main_w], self._attr("accent"))

        writers = self.session.typing_in(self.active, now)
        typing = ""
        if len(writers) == 1:
            typing = f"{writers[0]} is writing a message..."
        elif writers:
            typing = f"{', '.join(writers)} are writing..."
        self._put(stdscr, rows - 3, left, typing[:main_w], self._attr("muted"))

        prompt = f"[{self.title_of(self.active)}] "
        room = max(4, main_w - len(prompt) - 1)
        offset = max(0, self.cursor - room)
        self._put(stdscr, rows - 2, left, prompt[:main_w], self._attr("muted"))
        self._put(stdscr, rows - 2, left + len(prompt), self.buffer[offset:][:room])

        self._put(stdscr, rows - 1, 0,
                  self.client.status_summary(now, self.active)[:cols],
                  self._attr("muted"))

        try:
            stdscr.move(rows - 2,
                        min(cols - 1, left + len(prompt) + self.cursor - offset))
        except curses.error:
            pass
        stdscr.refresh()

    def _draw_sidebar(self, stdscr, rows: int, now: float) -> None:
        s = self.session
        lines: list[list[tuple[str, object]]] = [
            [(s.nick, s.colour)],
            [(s.status.label, "muted")],
            [(s.psm, "muted")],
            [(s.address, "muted")],
            [("", "muted")],
        ]
        for key in self.order():
            unread = self.unread.get(key, 0)
            active = key == self.active
            marker = ("\u25b8 " if active else "  ", "accent")
            if key == GROUP:
                row = [marker, ("everyone", "accent" if active else "text")]
                if unread:
                    row.append((f" ({unread})", "accent"))
                lines.append(row)
                lines.append([(f"    {len(s.online_peers())} online", "muted")])
            else:
                peer = s.peers.get(key)
                if peer is None:
                    continue
                row = [marker, (peer.status.glyph + " ", peer.colour),
                       (peer.label, "accent" if active else peer.colour)]
                if unread:
                    row.append((f" ({unread})", "accent"))
                lines.append(row)
                lines.append([(f"    {peer.status.label}", "muted")])
                if peer.psm:
                    lines.append([(f"    {peer.psm}", "muted")])

        focus = s.peers.get(self.active)
        if focus is not None:
            lines += [
                [("", "muted")],
                [(f"signal {proto.signal_bars(focus.rssi_dbm)}"
                  + (f"  {focus.rssi_dbm} dBm" if focus.rssi_dbm is not None else ""),
                  "muted")],
                [(proto.sparkline(
                    focus.rssi_history, SIDEBAR_W - 2,
                    proto.SPARK_BLOCKS if unicode_ok() else proto.SPARK_ASCII,
                 ), "rule")],
                [(f"last heard {s.last_seen_text(focus, now)}", "muted")],
            ]
        if s.queued_count():
            lines += [[("", "muted")], [(f"{s.queued_count()} queued", "warn")]]

        for y, segments in enumerate(lines):
            if y >= rows - 1:
                break
            x = 0
            for text, role in segments:
                if not text or x >= SIDEBAR_W:
                    continue
                self._put(stdscr, y, x, text[:SIDEBAR_W - x], self._attr(role))
                x += len(text)

    @staticmethod
    def _put(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
        if not text:
            return
        try:
            stdscr.addnstr(y, x, text, max(0, stdscr.getmaxyx()[1] - x - 1), attr)
        except curses.error:
            pass

    @staticmethod
    def _attr(role) -> int:
        """`role` is a name from ROLES, an int for a person, or "text"."""
        if not curses.has_colors():
            return curses.A_DIM if role == "muted" else curses.A_NORMAL
        if role == "text":
            return curses.color_pair(0)
        if role in ROLES:
            attr = curses.color_pair(9 + ROLES.index(role))
            return attr | curses.A_DIM if role in ("muted", "rule") else attr
        return curses.color_pair((int(role) % len(PALETTE)) + 1)

    # -- input -------------------------------------------------------------

    def find_peer(self, name: str) -> str | None:
        name = name.strip().lower()
        for addr, peer in self.session.peers.items():
            if peer.label.lower() == name or addr == name:
                return addr
        for addr, peer in self.session.peers.items():
            if peer.label.lower().startswith(name):
                return addr
        return None

    def handle_key(self, key: int) -> None:
        now = time.time()

        if key == 17:                                    # ctrl+q
            self.running = False
        elif key == 9:                                   # tab
            self.cycle(1)
        elif key == curses.KEY_BTAB:
            self.cycle(-1)
        elif key in (curses.KEY_ENTER, 10, 13):
            text, self.buffer, self.cursor = self.buffer.strip(), "", 0
            if text.startswith("/"):
                self.command(text, now)
            elif text:
                self.absorb(self.session.compose(text, self.active, now))
                self.scroll = 0
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            if self.cursor > 0:
                self.buffer = self.buffer[: self.cursor - 1] + self.buffer[self.cursor:]
                self.cursor -= 1
        elif key == curses.KEY_DC:
            self.buffer = self.buffer[: self.cursor] + self.buffer[self.cursor + 1:]
        elif key == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_RIGHT:
            self.cursor = min(len(self.buffer), self.cursor + 1)
        elif key == curses.KEY_HOME:
            self.cursor = 0
        elif key == curses.KEY_END:
            self.cursor = len(self.buffer)
            self.scroll = 0
        elif key == curses.KEY_PPAGE:
            self.scroll += 5
            return
        elif key == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - 5)
            return
        elif key == 21:                                  # ctrl+u
            self.buffer, self.cursor = "", 0
        elif 32 <= key < 0x110000:
            try:
                char = chr(key)
            except ValueError:
                return
            self.buffer = self.buffer[: self.cursor] + char + self.buffer[self.cursor:]
            self.cursor += 1

        self.absorb(self.session.on_keystroke(bool(self.buffer), self.active, now))

    def command(self, text: str, now: float) -> None:
        parts = text.split(" ", 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        statuses = {
            "/away": Status.AWAY, "/busy": Status.BUSY, "/brb": Status.BRB,
            "/back": Status.ONLINE, "/online": Status.ONLINE,
        }

        if cmd in statuses:
            self.absorb(self.session.set_status(statuses[cmd], now))
        elif cmd == "/psm":
            self.absorb(self.session.set_psm(arg))
        elif cmd == "/nick":
            if arg:
                self.session.nick = proto.sanitize(arg)
                self.session._force_heartbeat = True
                self.session._send_hello = True
        elif cmd in ("/colour", "/color"):
            if arg.isdigit():
                self.session.colour = int(arg) % len(PALETTE)
                self.session._force_heartbeat = True
                self.system(f"Your colour is now {PALETTE[self.session.colour]}.")
        elif cmd == "/g":
            self.switch(GROUP)
        elif cmd in ("/w", "/msg", "/dm"):
            addr = self.find_peer(arg)
            if addr is None:
                self.system(f"No contact matching '{arg}'.")
            else:
                self.session.conversation(addr)
                self.switch(addr)
        elif cmd == "/whois":
            addr = self.find_peer(arg) if arg else self.active
            if addr and addr in self.session.peers:
                from .crypto import fingerprint
                key = self.session.keyring.peer_keys.get(addr)
                self.system(
                    f"{self.session.peers[addr].label}: address {addr}, "
                    f"fingerprint {fingerprint(key) if key else 'unknown'}"
                )
                self.system("Compare that fingerprint out of band to be sure.")
            else:
                self.system("Usage: /whois <name>")
        elif cmd == "/clear":
            self.entries(self.active).clear()
        elif cmd == "/status":
            radio = self.client.link.lora
            if radio is not None:
                self.system(f"Radio: {radio.config.describe()}")
            self.system(self.client.status_summary(now, self.active))
            self.system(
                f"You are {self.session.address}, fingerprint "
                f"{self.session.identity.fingerprint}. "
                f"{len(self.session.peers)} contact(s), "
                f"{self.session.queued_count()} message(s) waiting to go out, "
                f"{self.session.keyring.failures} packet(s) heard but not for you."
            )
        elif cmd == "/help":
            for line in HELP:
                self.system(line)
        elif cmd == "/quit":
            self.running = False
        else:
            self.system(f"Unknown command: {cmd}")


def init_colours() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK

    # The terminal's background belongs to the person using it, so it is never
    # painted over; only foregrounds are set, against whatever is already there.
    palette = XTERM if curses.COLORS >= 256 else BASIC
    for i, colour in enumerate(palette["person"]):
        curses.init_pair(i + 1, colour, bg)
    for i, role in enumerate(ROLES):
        curses.init_pair(9 + i, palette[role], bg)


def run(client: Client, warnings: list[str]) -> None:
    locale.setlocale(locale.LC_ALL, "")

    def main(stdscr):
        try:
            curses.curs_set(1)
        except curses.error:
            pass          # some terminals cannot control cursor visibility
        init_colours()
        stdscr.timeout(100)
        stdscr.keypad(True)

        ui = CursesUI(client)
        session = client.session
        radio = client.link.lora
        ui.brand(f"loraline. You are {session.nick}, and your address is "
                 f"{session.address}.")
        if radio is not None:
            ui.system(f"Listening on {radio.config.channel + 850} MHz. "
                      f"Everyone you talk to has to be set the same.")
        for note in client.notes:
            ui.system(note)
        for warning in warnings:
            ui.system(warning, level="warn")
        ui.system("A tick after your message means it went out. Two means it "
                  "arrived. A dot means it is still waiting.")
        ui.system("Tab moves between conversations. Type /help for the rest.")

        while ui.running:
            ui.absorb(client.pump())
            ui.draw(stdscr)
            try:
                key = stdscr.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                break
            if isinstance(key, str):
                key = ord(key)
            ui.handle_key(key)

    curses.wrapper(main)
