"""Pluggable bearers, and the link that bridges between them.

An interface is anything that can carry a line of bytes to peers: a LoRa
module on a serial port, a TCP socket, whatever comes next. A node can hold
several at once, which is what lets one machine sit between a radio and the
internet.

Bridging happens on sealed envelopes, before decryption. The bridge relays a
direct message between two of its peers without being able to read a word of
it, which is the property that makes this worth doing at all.
"""

from __future__ import annotations

import queue
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover
    serial = None

from . import protocol as proto
from .protocol import DutyBudget, Frame, LineReader

HF_BASE_MHZ = 850
HF_MAX_CHANNEL = 80
DEDUP_MEMORY = 512
FORWARD_QUEUE = 64


def channel_for_mhz(mhz: int) -> int:
    chan = mhz - HF_BASE_MHZ
    if not 0 <= chan <= HF_MAX_CHANNEL:
        raise ValueError(f"{mhz} MHz is outside the HF module's 850-930 MHz range")
    return chan


def mhz_for_channel(chan: int) -> int:
    return HF_BASE_MHZ + chan


@dataclass
class Packet:
    """One sealed line, as it arrived, plus where from."""
    line: bytes
    interface: str
    rssi_raw: int | None = None


@dataclass
class RadioConfig:
    """Every field must match on every LoRa unit or they will not hear each other."""

    channel: int = 65
    sf: int = 10
    bw_khz: int = 125
    cr: int = 1
    power_dbm: int = 22
    address: int = 258
    netid: int = 0
    lbt: bool = True
    rssi: bool = True

    BW_CODES = {125: 0, 250: 1, 500: 2}

    def at_commands(self) -> list[str]:
        return [
            f"AT+SF={self.sf}", f"AT+BW={self.BW_CODES[self.bw_khz]}",
            f"AT+CR={self.cr}", f"AT+PWR={self.power_dbm}",
            f"AT+NETID={self.netid}", f"AT+ADDR={self.address}",
            f"AT+TXCH={self.channel}", f"AT+RXCH={self.channel}",
            f"AT+LBT={1 if self.lbt else 0}", f"AT+RSSI={1 if self.rssi else 0}",
            "AT+MODE=1",
        ]

    def airtime_of(self, frame_bytes: int) -> float:
        return proto.airtime_ms(frame_bytes + proto.MODULE_OVERHEAD_BYTES,
                                sf=self.sf, bw_khz=self.bw_khz, cr=self.cr)

    def describe(self) -> str:
        return (f"{mhz_for_channel(self.channel)} MHz (ch {self.channel})  "
                f"SF{self.sf}  BW{self.bw_khz}  CR4/{self.cr + 4}  "
                f"{self.power_dbm} dBm  addr {self.address}")


class Interface:
    """Base bearer. Received lines land on `inbox` as Packets."""

    name = "iface"
    metered = False

    def __init__(self) -> None:
        self.inbox: "queue.Queue[Packet | Exception]" = queue.Queue()

    def start(self) -> None: ...
    def close(self) -> None: ...
    def can_send(self, nbytes: int) -> bool: return True
    def send(self, line: bytes) -> float: return 0.0
    def status(self) -> str: return self.name


class LoRaInterface(Interface):
    """A Waveshare USB-TO-LoRa module in stream mode, with airtime accounting."""

    metered = True

    def __init__(self, port: str, config: RadioConfig, baud: int = 115200,
                 duty_limit_percent: float = 1.0) -> None:
        super().__init__()
        if serial is None:
            raise RuntimeError("pyserial is not installed. Try: pip install pyserial")
        self.name = "lora"
        self.port = port
        self.config = config
        self.baud = baud
        self.budget = DutyBudget(duty_limit_percent)
        self._ser = None
        self._reader = LineReader()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def open(self) -> None:
        self._ser = serial.Serial(self.port, self.baud, timeout=0.2)
        time.sleep(0.3)
        self._ser.reset_input_buffer()

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
        if self._ser is not None and self._ser.is_open:
            self._ser.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._ser.read(self._ser.in_waiting or 1)
            except Exception as exc:
                self.inbox.put(exc)
                return
            if data:
                for line, rssi in self._reader.feed(data):
                    self.inbox.put(Packet(line, self.name, rssi))

    def apply_config(self, verbose: bool = False) -> list[str]:
        """Enter AT mode, push settings, exit. Settings apply only on exit."""
        log: list[str] = []

        def cmd(text: str, wait: float = 0.35) -> str:
            self._ser.reset_input_buffer()
            self._ser.write((text + "\r\n").encode())
            self._ser.flush()
            time.sleep(wait)
            reply = self._ser.read(self._ser.in_waiting or 1).decode(errors="replace").strip()
            log.append(f"{text}  ->  {reply or '(no reply)'}")
            if verbose:
                print(log[-1])
            return reply

        cmd("+++", wait=0.6)
        cmd("AT+VER")
        for c in self.config.at_commands():
            cmd(c)
        cmd("AT+EXIT", wait=0.6)
        time.sleep(0.5)
        self._ser.reset_input_buffer()
        return log

    def can_send(self, nbytes: int) -> bool:
        return self.budget.can_send(self.config.airtime_of(nbytes))

    def send(self, line: bytes) -> float:
        cost = self.config.airtime_of(len(line))
        with self._lock:
            self._ser.write(line)
            self._ser.flush()
        self.budget.record(cost)
        return cost

    def status(self) -> str:
        mhz = mhz_for_channel(self.config.channel)
        remaining = self.budget.remaining_ms()
        if remaining == float("inf"):
            return f"{mhz} MHz   no hourly limit here"
        return f"{mhz} MHz   {remaining / 1000:.0f}s of radio time left this hour"


class _SocketBearer(Interface):
    """Shared plumbing for the TCP interfaces: one reader per connection."""

    def __init__(self) -> None:
        super().__init__()
        self._socks: dict[socket.socket, LineReader] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def _serve(self, sock: socket.socket, label: str) -> None:
        reader = LineReader()
        with self._lock:
            self._socks[sock] = reader
        try:
            while not self._stop.is_set():
                data = sock.recv(4096)
                if not data:
                    break
                for line, _ in reader.feed(data):
                    self.inbox.put(Packet(line, self.name, None))
        except OSError:
            pass
        finally:
            with self._lock:
                self._socks.pop(sock, None)
            try:
                sock.close()
            except OSError:
                pass

    def can_send(self, nbytes: int) -> bool:
        """A bearer with nobody on the other end cannot carry anything.

        Without this, a frame written to a socket that has not connected yet
        is silently dropped, and the session believes it was transmitted.
        """
        return self.connections > 0

    def send(self, line: bytes) -> float:
        with self._lock:
            targets = list(self._socks)
        for sock in targets:
            try:
                sock.sendall(line)
            except OSError:
                with self._lock:
                    self._socks.pop(sock, None)
        return 0.0

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            targets = list(self._socks)
        for sock in targets:
            try:
                sock.close()
            except OSError:
                pass

    @property
    def connections(self) -> int:
        with self._lock:
            return len(self._socks)


class TCPServerInterface(_SocketBearer):
    """Listens. Anyone who connects joins the same conversation."""

    def __init__(self, host: str = "0.0.0.0", port: int = 4242) -> None:
        super().__init__()
        self.name = "tcp-listen"
        self.host, self.port = host, port
        self._listener: socket.socket | None = None

    def start(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((self.host, self.port))
        self._listener.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                sock, addr = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock, str(addr)),
                             daemon=True).start()

    def close(self) -> None:
        super().close()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass

    def status(self) -> str:
        n = self.connections
        return f"{n} joined over the internet" if n else "nobody joined yet"


class TCPClientInterface(_SocketBearer):
    """Connects out, and keeps trying if the far end goes away."""

    def __init__(self, host: str, port: int, retry_s: float = 5.0) -> None:
        super().__init__()
        self.name = "tcp"
        self.host, self.port, self.retry_s = host, port, retry_s

    def start(self) -> None:
        threading.Thread(target=self._dial, daemon=True).start()

    def _dial(self) -> None:
        while not self._stop.is_set():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=10)
                sock.settimeout(None)
                self._serve(sock, f"{self.host}:{self.port}")
            except OSError:
                pass
            if self._stop.is_set():
                return
            time.sleep(self.retry_s)

    def status(self) -> str:
        return ("connected to " if self.connections else "trying to reach ") + self.host


class Link:
    """Seals frames, spreads them across every bearer, and bridges between them."""

    def __init__(self, interfaces: list[Interface], keyring=None,
                 bridge: bool = True) -> None:
        self.interfaces = interfaces
        self.keyring = keyring
        self.bridge = bridge
        self._seen: deque[int] = deque(maxlen=DEDUP_MEMORY)
        self._seen_set: set[int] = set()
        self._connections: dict[str, int] = {}
        self.forwarded = 0
        self.undeliverable = 0

    # -- bookkeeping -------------------------------------------------------

    def _remember(self, line: bytes) -> bool:
        """True if this line is new. Dedup is what stops a bridge looping."""
        digest = hash(line)
        if digest in self._seen_set:
            return False
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(digest)
        self._seen_set.add(digest)
        return True

    @property
    def lora(self) -> LoRaInterface | None:
        for iface in self.interfaces:
            if isinstance(iface, LoRaInterface):
                return iface
        return None

    @property
    def budget(self) -> DutyBudget:
        lora = self.lora
        return lora.budget if lora else DutyBudget(0.0)

    # -- receive -----------------------------------------------------------

    def new_bearer_up(self) -> bool:
        """True once each time a bearer gains a connection.

        A frame is only offered to the bearers that could carry it at the
        time, so anything sent while a socket was down never reached the far
        side. When a link comes up the caller should re-announce rather than
        wait out the heartbeat interval.
        """
        changed = False
        for iface in self.interfaces:
            count = getattr(iface, "connections", None)
            if count is None:
                continue
            if count > self._connections.get(iface.name, 0):
                changed = True
            self._connections[iface.name] = count
        return changed

    def pump(self) -> tuple[list[Frame], list[Exception]]:
        frames: list[Frame] = []
        errors: list[Exception] = []

        for iface in self.interfaces:
            while True:
                try:
                    item = iface.inbox.get_nowait()
                except queue.Empty:
                    break
                if isinstance(item, Exception):
                    errors.append(item)
                    continue
                if not self._remember(item.line):
                    continue          # already handled, probably came back round

                if self.bridge:
                    self._relay(item)

                frame = proto.decode_line(item.line, self.keyring)
                if frame is not None:
                    frame.rssi_raw = item.rssi_raw
                    frame.via = item.interface
                    frames.append(frame)
        return frames, errors

    def _relay(self, packet: Packet) -> None:
        """Pass a sealed line to every other bearer.

        No decryption happens here, so a bridge relays direct messages between
        two of its peers without being able to read them. Forwarded traffic
        still costs airtime on a metered bearer, so it can be refused.
        """
        raw = packet.line + b"\n"
        for iface in self.interfaces:
            if iface.name == packet.interface:
                continue
            if not iface.can_send(len(raw)):
                self.undeliverable += 1
                continue
            try:
                iface.send(raw)
                self.forwarded += 1
            except Exception:
                self.undeliverable += 1

    # -- transmit ----------------------------------------------------------

    def wire_frame(self, frame: Frame, dst: str = "*") -> Frame:
        return proto.seal(frame, self.keyring, dst)

    def can_send(self, frame: Frame, dst: str = "*") -> bool:
        size = self.wire_frame(frame, dst).size
        return any(i.can_send(size) for i in self.interfaces)

    def send(self, frame: Frame, dst: str = "*") -> float:
        line = self.wire_frame(frame, dst).encode()
        self._remember(line)       # do not relay our own transmission back out
        cost = 0.0
        for iface in self.interfaces:
            if iface.can_send(len(line)):
                try:
                    cost = max(cost, iface.send(line))
                except Exception:
                    self.undeliverable += 1
        return cost

    def payload_budget(self) -> int:
        return proto.payload_budget(self.keyring)

    def status(self) -> str:
        return "  ".join(i.status() for i in self.interfaces)

    def close(self) -> None:
        for iface in self.interfaces:
            iface.close()
