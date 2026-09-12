#!/usr/bin/env python3
"""Start the built app and check it actually serves and works.

A build that produces a file is not a build that produces a program. This is
what catches a bundle missing pyserial's backend: it starts perfectly well and
falls over the moment somebody asks it to look for a radio.

Deliberately simple. It is the thing that tells you the complicated thing is
broken, so it must not be capable of hanging itself.
"""
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PORT = 8737
GIVE_UP_AFTER = 60.0


def binary() -> Path:
    dist = Path(__file__).resolve().parent.parent / "dist"
    if platform.system() == "Darwin":
        inside = dist / "loraline.app" / "Contents" / "MacOS" / "loraline"
        if inside.exists():
            return inside
    if platform.system() == "Windows":
        return dist / "loraline.exe"
    return dist / "loraline"


def fetch(path: str, body=None, timeout: float = 4.0):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    return urllib.request.urlopen(url, data=data, timeout=timeout)


def one_snapshot(timeout: float = 6.0):
    """Read one event off the stream and hang up.

    The socket gets its own timeout: a stream that stays open and says nothing
    is the one way a check like this turns into a build that never finishes.
    """
    try:
        stream = fetch("/events", timeout=timeout)
    except Exception:
        return None
    try:
        stream.fp.raw._sock.settimeout(timeout)
    except Exception:
        pass
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            # Line by line. Asking for a fixed number of bytes waits until it
            # has that many, and an event is smaller than any sensible buffer,
            # so the read blocks until the next keep-alive fifteen seconds
            # later and the check appears to hang.
            try:
                line = stream.readline()
            except (socket.timeout, OSError):
                return None
            if not line:
                return None
            if line.startswith(b"data: "):
                try:
                    return json.loads(line[6:])
                except Exception:
                    return None
    finally:
        try:
            stream.close()
        except Exception:
            pass
    return None


def main() -> int:
    app = binary()
    if not app.exists():
        print(f"no such build: {app}", file=sys.stderr)
        return 1
    print(f"starting {app} ({app.stat().st_size / 1e6:.1f} MB)")

    room = tempfile.mkdtemp()
    env = dict(os.environ, HOME=room, USERPROFILE=room, BROWSER="echo")
    process = subprocess.Popen([str(app)], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    started = time.time()
    try:
        page = None
        while time.time() - started < GIVE_UP_AFTER:
            if process.poll() is not None:
                said = process.stdout.read().decode("utf-8", "replace")
                print("it exited on its own:\n" + said, file=sys.stderr)
                return 1
            try:
                page = fetch("/", timeout=2).read().decode()
                break
            except Exception:
                time.sleep(1)
        if not page:
            print("it never served anything", file=sys.stderr)
            return 1
        if "loraline" not in page:
            print("it served something that is not its page", file=sys.stderr)
            return 1
        print(f"served its page, {len(page)} bytes")

        # Looking for a radio is what exercises pyserial inside the bundle.
        fetch("/", {"do": "look"})
        found = None
        while time.time() - started < GIVE_UP_AFTER:
            snapshot = one_snapshot()
            if snapshot and not snapshot.get("busy"):
                found = snapshot
                break
            time.sleep(1)
        if not found:
            print("it never finished looking for a radio", file=sys.stderr)
            return 1
        print(f"looked for a radio: {len(found.get('ports', []))} port(s), "
              f"bands {[b['key'] for b in found.get('bands', [])]}")

        # And that it can set itself up, make an identity and remember.
        fetch("/", {"do": "begin", "nick": "smoke", "passphrase": "test",
                    "band": "eu868", "port": ""})
        ready = None
        while time.time() - started < GIVE_UP_AFTER:
            snapshot = one_snapshot()
            if snapshot and snapshot.get("ready"):
                ready = snapshot
                break
            time.sleep(1)
        if not ready:
            print("it never finished starting up", file=sys.stderr)
            return 1
        kept = Path(room) / ".loraline" / "settings.json"
        if not kept.exists():
            print("it did not remember its settings", file=sys.stderr)
            return 1
        print(f"started as {ready['me']['address']}, wrote {kept.name}")

        # Whatever rides on loraline has to be inside the bundle: a frozen
        # program cannot import something that was not there when it was
        # frozen, and a download with an empty switcher does nothing.
        #
        # The test is against what was actually next door when the build ran,
        # not against a list. loraline on its own is a chat client, which is a
        # perfectly reasonable thing to build and the only thing there is to
        # build before the other repositories exist.
        beside = Path(__file__).resolve().parent.parent.parent
        expected = {name for name in ("catacomms", "longshore", "hearsay")
                    if (beside / name / name / "__init__.py").exists()}
        riding = {p["title"] for p in ready.get("panels", [])}
        if expected - riding:
            print(f"checked out but not in the bundle: "
                  f"{', '.join(sorted(expected - riding))}", file=sys.stderr)
            return 1
        if not expected:
            print("nothing riding along; a chat client on its own")
            return 0
        for panel in ready.get("panels", []):
            page = fetch(panel["route"], timeout=4).read().decode()
            if len(page) < 500:
                print(f"{panel['route']} served almost nothing", file=sys.stderr)
                return 1
            if "loraline-nav" not in page:
                print(f"{panel['route']} has no switcher on it", file=sys.stderr)
                return 1
        print("carrying: " + ", ".join(
            p["title"] + (" (always on)" if p["always"] else "")
            for p in ready.get("panels", [])))
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except Exception:
            process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
