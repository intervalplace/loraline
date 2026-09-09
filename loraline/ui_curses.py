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

CURSES_COLOURS = {
    "green": curses.COLOR_GREEN, "cyan": curses.COLOR_CYAN,
    "magenta": curses.COLOR_MAGENTA, "yellow": curses.COLOR_YELLOW,
    "blue": curses.COLOR_BLUE, "red": curses.COLOR_RED,
    "white": curses.COLOR_WHITE, "teal": curses.COLOR_CYAN,
}

HELP = (
    "tab switches conversation  |  /g group  |  /w <name> open a private chat  |  "
    "/away /busy /brb /back  |  /psm <text>  |  /colour <0-7>  |  /whois <name>  |  "
    "/status  |  /quit    (ctrl+q quits, pgup scrolls back)"
)


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

    def wrap_entries(self, width: int) -> list[tuple[str, int, int]]:
        out: list[tuple[str, int, int]] = []
        for entry in self.entries(self.active):
            if entry.kind == "system":
                prefix = "* "
                colour, dim = (-2 if entry.level == "warn" else -1), 1
                body = entry.text
            else:
                stamp = time.strftime("%H:%M", time.localtime(entry.when))
                mark = ""
                if entry.seq is not None:
                    item = self.session.outgoing(entry.seq)
                    if item is not None:
                        mark = " " + item.resolve().glyph + item.tally()
                prefix = f"{stamp} {entry.who}: "
                colour, dim = entry.colour, 0
                body = entry.text + mark

            indent = " " * min(len(prefix), max(0, width // 3))
            lines = self._wrap(body, max(8, width - len(prefix)))
            out.append((prefix + lines[0], colour, dim))
            for line in lines[1:]:
                out.append((indent + line, colour, dim))
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
                self._put(stdscr, y, SIDEBAR_W, "\u2502", self._pair(-1))

        header = f"\u2500\u2500 {self.title_of(self.active)} "
        if self.active != GROUP:
            peer = self.session.peers.get(self.active)
            if peer is not None:
                lock = "e2e" if self.session.keyring.knows(self.active) else "no key"
                header += f"[{peer.status.label}, {lock}] "
        self._put(stdscr, 0, left, (header + "\u2500" * main_w)[:main_w],
                  self._pair(-1) | curses.A_DIM)

        convo_h = max(1, rows - 4)
        lines = self.wrap_entries(main_w)
        self.scroll = max(0, min(self.scroll, max(0, len(lines) - convo_h)))
        end = len(lines) - self.scroll
        for i, (text, colour, dim) in enumerate(lines[max(0, end - convo_h):end]):
            attr = self._pair(colour) | (curses.A_DIM if dim else 0)
            self._put(stdscr, i + 1, left, text[:main_w], attr)

        if self.scroll > 0:
            note = f"-- scrolled back {self.scroll} lines, End to return --"
            self._put(stdscr, convo_h, left, note[:main_w],
                      self._pair(-1) | curses.A_REVERSE)

        writers = self.session.typing_in(self.active, now)
        typing = ""
        if len(writers) == 1:
            typing = f"{writers[0]} is writing a message..."
        elif writers:
            typing = f"{', '.join(writers)} are writing..."
        self._put(stdscr, rows - 3, left, typing[:main_w], self._pair(-1) | curses.A_DIM)

        prompt = f"[{self.title_of(self.active)}] "
        room = max(4, main_w - len(prompt) - 1)
        offset = max(0, self.cursor - room)
        self._put(stdscr, rows - 2, left, prompt[:main_w], self._pair(-1) | curses.A_DIM)
        self._put(stdscr, rows - 2, left + len(prompt), self.buffer[offset:][:room])

        self._put(stdscr, rows - 1, 0,
                  self.client.status_summary(now, self.active).ljust(cols)[:cols],
                  self._pair(-3) | curses.A_REVERSE)

        try:
            stdscr.move(rows - 2,
                        min(cols - 1, left + len(prompt) + self.cursor - offset))
        except curses.error:
            pass
        stdscr.refresh()

    def _draw_sidebar(self, stdscr, rows: int, now: float) -> None:
        s = self.session
        lines: list[tuple[str, int, int, bool]] = [
            (s.nick, s.colour, 0, False),
            (s.status.label, -1, 1, False),
            (s.psm, -1, 1, False),
            (s.address, -1, 1, False),
            ("", -1, 0, False),
        ]
        for key in self.order():
            unread = self.unread.get(key, 0)
            badge = f" ({unread})" if unread else ""
            active = key == self.active
            if key == GROUP:
                online = len(s.online_peers())
                lines.append((f"\u25a0 everyone{badge}", -1, 0, active))
                lines.append((f"  {online} online", -1, 1, False))
            else:
                peer = s.peers.get(key)
                if peer is None:
                    continue
                lines.append((f"{peer.status.glyph} {peer.label}{badge}",
                              peer.colour, 0, active))
                lines.append((f"  {peer.status.label}", -1, 1, False))
                if peer.psm:
                    lines.append((f"  {peer.psm}", -1, 1, False))

        focus = s.peers.get(self.active)
        if focus is not None:
            lines += [
                ("", -1, 0, False),
                (f"signal {proto.signal_bars(focus.rssi_dbm)}", -1, 1, False),
                (proto.sparkline(focus.rssi_history, SIDEBAR_W - 1), -1, 1, False),
                (f"heard {s.last_seen_text(focus, now)}", -1, 1, False),
            ]
        if s.queued_count():
            lines += [("", -1, 0, False), (f"{s.queued_count()} queued", -2, 0, False)]

        for y, (text, colour, dim, active) in enumerate(lines):
            if y >= rows - 1 or not text:
                continue
            attr = self._pair(colour)
            if dim:
                attr |= curses.A_DIM
            if active:
                attr |= curses.A_REVERSE
            self._put(stdscr, y, 0, text[:SIDEBAR_W].ljust(SIDEBAR_W) if active
                      else text[:SIDEBAR_W], attr)

    @staticmethod
    def _put(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
        if not text:
            return
        try:
            stdscr.addnstr(y, x, text, max(0, stdscr.getmaxyx()[1] - x - 1), attr)
        except curses.error:
            pass

    @staticmethod
    def _pair(index: int) -> int:
        if not curses.has_colors():
            return curses.A_NORMAL
        if index == -1:
            return curses.color_pair(0)
        if index == -2:
            return curses.color_pair(10)
        if index == -3:
            return curses.color_pair(11)
        return curses.color_pair((index % len(PALETTE)) + 1)

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
            self.system(self.client.link.status())
            self.system(self.client.status_summary(now, self.active))
            self.system(
                f"you are {self.session.address} ({self.session.identity.fingerprint})  "
                f"peers {len(self.session.peers)}  queued {self.session.queued_count()}  "
                f"unconfirmed {self.session.unconfirmed_count()}  "
                f"undecryptable {self.session.keyring.failures}"
            )
        elif cmd == "/help":
            self.system(HELP)
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
    for i, name in enumerate(PALETTE):
        curses.init_pair(i + 1, CURSES_COLOURS.get(name, curses.COLOR_WHITE), bg)
    curses.init_pair(10, curses.COLOR_YELLOW, bg)
    curses.init_pair(11, curses.COLOR_WHITE, bg)


def run(client: Client, warnings: list[str]) -> None:
    locale.setlocale(locale.LC_ALL, "")

    def main(stdscr):
        curses.curs_set(1)
        init_colours()
        stdscr.timeout(100)
        stdscr.keypad(True)

        ui = CursesUI(client)
        ui.system(f"Signed in as {client.session.nick} ({client.session.address}).")
        ui.system(client.link.status())
        for note in client.notes:
            ui.system(note)
        for warning in warnings:
            ui.system(warning, level="warn")
        ui.system(HELP)

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
