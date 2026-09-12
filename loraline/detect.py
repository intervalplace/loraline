"""Finding the module.

Asking somebody to run `ls /dev/cu.*`, work out which entry appeared when they
plugged something in, and then type it correctly is most of why loraline had a
thirty minute setup. The computer already knows: there are a handful of serial
ports, and exactly one of them answers AT with OK.

So: open each, say hello, see who replies. It takes a second and it cannot
really be got wrong, which matters more than the second.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

PROBE_BAUD = 115200
PROBE_WAIT = 0.45        # long enough for a module to answer, short enough to try six

# Ports that are never going to be a radio. Probing a Mac's Bluetooth serial
# port is harmless but slow, and on some machines it hangs.
NEVER = ("/dev/cu.Bluetooth", "/dev/tty.Bluetooth", "/dev/cu.debug",
         "/dev/cu.wlan", "BLTH")

# Chips these modules are built on. A match is a strong hint rather than a
# requirement: the probe decides, this only orders the queue.
LIKELY = ("CH340", "CH341", "CH9102", "CP210", "FTDI", "FT232", "USB Serial",
          "USB-SERIAL", "Silicon Labs", "wchusbserial", "usbserial")


@dataclass
class Found:
    port: str
    description: str = ""
    answered: bool = False        # did it actually reply to AT

    @property
    def label(self) -> str:
        if self.description and self.description != "n/a":
            return f"{self.port} ({self.description})"
        return self.port


def candidates() -> list:
    """Every serial port worth trying, likeliest first."""
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    out = []
    for entry in list_ports.comports():
        name = entry.device or ""
        if any(bad.lower() in name.lower() for bad in NEVER):
            continue
        # macOS shows every port twice, as tty and cu. Only cu is usable:
        # opening the tty one blocks waiting for carrier detect, which never
        # comes, and the program appears to hang.
        if sys.platform == "darwin" and "/dev/tty." in name:
            continue
        text = " ".join(filter(None, (entry.description, entry.manufacturer,
                                      entry.product, name)))
        rank = 0 if any(hint.lower() in text.lower() for hint in LIKELY) else 1
        out.append((rank, Found(port=name, description=entry.description or "")))
    return [found for _, found in sorted(out, key=lambda pair: (pair[0], pair[1].port))]


def answers(port: str, wait: float = PROBE_WAIT) -> bool:
    """Does something on this port reply to AT?

    Opening a port can fail for a dozen boring reasons: busy, gone, no
    permission. All of them mean the same thing here, which is that this is
    not the radio.
    """
    try:
        import serial
    except Exception:
        return False
    handle = None
    try:
        handle = serial.Serial(port, PROBE_BAUD, timeout=wait, write_timeout=wait)
        time.sleep(0.05)
        handle.reset_input_buffer()
        handle.write(b"AT\r\n")
        handle.flush()
        deadline = time.time() + wait
        seen = b""
        while time.time() < deadline:
            seen += handle.read(32)
            if b"OK" in seen or b"+" in seen:
                return True
            if not seen and time.time() > deadline - 0.05:
                break
        return b"OK" in seen
    except Exception:
        return False
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


def find(probe: bool = True) -> list:
    """Ports that look like a radio, the ones that answered first.

    Returns everything rather than picking, because a person with two modules
    plugged in should be told there are two rather than have one chosen for
    them.
    """
    found = candidates()
    if not probe:
        return found
    for entry in found:
        entry.answered = answers(entry.port)
    return sorted(found, key=lambda f: (not f.answered, f.port))


def only_one(probe: bool = True):
    """The radio, if there is exactly one obvious answer. Otherwise None, and
    the person gets to choose."""
    found = find(probe=probe)
    answered = [f for f in found if f.answered]
    if len(answered) == 1:
        return answered[0]
    if not answered and len(found) == 1:
        return found[0]        # nothing replied, but there is only one thing to try
    return None
