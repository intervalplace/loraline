"""Staying on.

The radio is a thing other people talk to. A program you have to remember to
open is one that is shut whenever somebody tries: messages arrive when you are
not looking, a key exchange needs you present, and if you are a bridge for a
friend over the internet then you being closed is them being cut off. hearsay
made this obvious because carrying is the whole of what it does, but it was
always true of the chat.

So this is a small service that holds the radio and keeps a window you can
open. Closing the window is not quitting.

Nothing here starts anything without being asked. An application that installs
itself into your login is a rude application, so it is offered once, plainly,
and it is one click to undo.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

APP = "loraline"
LABEL = "org.loraline.node"


def frozen() -> bool:
    """Running as a bundle rather than out of a source tree."""
    return getattr(sys, "frozen", False)


def command() -> list:
    """How to start this again. A bundle is its own command; a source tree
    needs the interpreter that is running it."""
    if frozen():
        return [sys.executable]
    return [sys.executable, "-m", APP]


@dataclass
class Autostart:
    """Whether this comes back on its own, and how to change that.

    Every platform has its own opinion and all three are a file in a place.
    None of them need a package, an installer or a privilege.
    """

    @property
    def where(self) -> Path:
        home = Path.home()
        if sys.platform == "darwin":
            return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        if sys.platform.startswith("win"):
            return (home / "AppData" / "Roaming" / "Microsoft" / "Windows"
                    / "Start Menu" / "Programs" / "Startup" / f"{APP}.cmd")
        return home / ".config" / "systemd" / "user" / f"{APP}.service"

    @property
    def on(self) -> bool:
        return self.where.exists()

    def body(self) -> str:
        run = command()
        if sys.platform == "darwin":
            args = "".join(f"    <string>{a}</string>\n" for a in run)
            return (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                f'  <key>Label</key><string>{LABEL}</string>\n'
                f'  <key>ProgramArguments</key><array>\n{args}  </array>\n'
                '  <key>RunAtLoad</key><true/>\n'
                # Restart if it falls over, but not in a tight loop: a radio
                # that has been unplugged should not spin the processor.
                '  <key>KeepAlive</key><dict>'
                '<key>SuccessfulExit</key><false/></dict>\n'
                '  <key>ThrottleInterval</key><integer>30</integer>\n'
                '</dict></plist>\n')
        if sys.platform.startswith("win"):
            quoted = " ".join(f'"{a}"' for a in run)
            # start /b so no console window appears behind the browser.
            return f'@echo off\r\nstart "" /b {quoted}\r\n'
        return (
            "[Unit]\n"
            "Description=loraline, holding the radio\n\n"
            "[Service]\n"
            f"ExecStart={' '.join(run)}\n"
            "Restart=on-failure\n"
            "RestartSec=30\n\n"
            "[Install]\n"
            "WantedBy=default.target\n")

    def turn_on(self) -> bool:
        try:
            self.where.parent.mkdir(parents=True, exist_ok=True)
            self.where.write_text(self.body())
            if sys.platform.startswith("win"):
                pass
            elif sys.platform == "darwin":
                subprocess.run(["launchctl", "load", "-w", str(self.where)],
                               capture_output=True, timeout=10)
            else:
                subprocess.run(["systemctl", "--user", "daemon-reload"],
                               capture_output=True, timeout=10)
                subprocess.run(["systemctl", "--user", "enable", f"{APP}.service"],
                               capture_output=True, timeout=10)
            return True
        except Exception:
            return False

    def turn_off(self) -> bool:
        try:
            if self.where.exists():
                if sys.platform == "darwin":
                    subprocess.run(["launchctl", "unload", "-w", str(self.where)],
                                   capture_output=True, timeout=10)
                elif not sys.platform.startswith("win"):
                    subprocess.run(["systemctl", "--user", "disable", f"{APP}.service"],
                                   capture_output=True, timeout=10)
                self.where.unlink()
            return True
        except Exception:
            return False

    def describe(self) -> str:
        if sys.platform == "darwin":
            return "a launch agent in ~/Library/LaunchAgents"
        if sys.platform.startswith("win"):
            return "a shortcut in your Startup folder"
        return "a systemd user service in ~/.config/systemd/user"


def only_one(port: int) -> bool:
    """True if nothing is already holding the radio.

    Two copies would fight over the serial port and the loser would look
    broken rather than second. The running one is already serving a page, so
    finding it is the same as asking whether the port answers.
    """
    import socket
    probe = socket.socket()
    try:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) != 0
    except Exception:
        return True
    finally:
        probe.close()


def raise_window(port: int) -> None:
    """Bring the one that is already running to the front."""
    import webbrowser
    try:
        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception:
        pass
