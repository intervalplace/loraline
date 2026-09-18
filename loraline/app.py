"""loraline without a terminal.

Three questions, asked once: what shall we call you, what is the passphrase,
and which band are you on. The module is found by asking every serial port
whether it is a radio, the band is applied without anybody typing an AT
command, and the second launch asks nothing at all.

The command line is still there and always will be. It is better for anybody
who wants to know what is happening. This is for everybody else, who is most
people, and whose absence from the other end of a radio is the only thing
wrong with the radio.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import webbrowser

from . import window as own_window
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import crypto, detect, face as faces, host as hosting, service, settings as store
from . import __version__ as _version, build_id as _build
from .client import Client
from .crypto import GROUP, Identity, Keyring
from .protocol import Delivery, Status
from .session import DeliveryEvent, MessageEvent, PresenceEvent, SystemEvent
from .transport import Link, LoRaInterface, RadioConfig


class App:
    """A little web server, a radio, and the glue."""

    def __init__(self, port: int = 8737) -> None:
        self.port = port
        self.inbox: "queue.Queue" = queue.Queue()
        self._listeners: list = []
        self._lock = threading.Lock()
        self._latest = "{}"
        self._server = None

        self.settings = store.load()
        self.client = None
        self.link = None
        self.identity = None
        self.log: list = []
        self.ports: list = []
        self.busy = ""
        self.trouble = ""
        self.convo = GROUP
        self.running = True
        # The session reports events; keeping the conversations is the
        # interface's job, exactly as it is in the terminal one.
        self.threads: dict = {}
        self.unread: dict = {}
        # Whatever else is installed. Found once, started when the radio is.
        self.panels = hosting.discover()
        self.host = None
        self.autostart = service.Autostart()
        self.watchers = 0          # how many windows are open on this
        self.faces = faces.Faces()
        self.arriving: dict = {}   # address -> pieces of their picture
        self._panel_trouble: dict = {}
        self._last_stumble = ""
        self.asked: dict = {}      # address -> when we last asked

    # -- serving -----------------------------------------------------------

    def start_server(self) -> None:
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def handle(self):
                try:
                    super().handle()
                except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                    pass

            def do_GET(self):
                if self.path.startswith("/events"):
                    return app._stream(self)
                body = app.page_for(self.path).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", "replace")
                try:
                    app.inbox.put(json.loads(raw))
                except Exception:
                    pass
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def _stream(self, handler) -> None:
        channel: "queue.Queue[str]" = queue.Queue(maxsize=32)
        with self._lock:
            self._listeners.append(channel)
            self.watchers = len(self._listeners)
            first = self._latest
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.end_headers()
        try:
            handler.wfile.write(f"data: {first}\n\n".encode())
            handler.wfile.flush()
            while True:
                try:
                    handler.wfile.write(f"data: {channel.get(timeout=15)}\n\n".encode())
                except queue.Empty:
                    handler.wfile.write(b": still here\n\n")
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self._lock:
                if channel in self._listeners:
                    self._listeners.remove(channel)
                self.watchers = len(self._listeners)

    def page_for(self, path: str) -> str:
        """Whose page is this? Everything shares one address and one stream;
        only the drawing differs."""
        route = "/" + path.lstrip("/").split("?")[0]
        body = PAGE
        for panel in self.panels:
            if not panel.owns(route):
                continue
            try:
                body = panel.page(route)
            except Exception as exc:
                # This used to fall back to the chat page without a word, so a
                # panel whose page() would not take the route it was given
                # served the wrong page for months and looked like a dead
                # switcher rather than a broken one.
                import traceback
                traceback.print_exc()
                self.note(f"{panel.title} could not draw its page: {exc}", "warn")
                body = _sorry(panel.title, exc)
            break
        return self.with_nav(body, route)

    def with_nav(self, page: str, here: str) -> str:
        """Put the switcher at the top of whatever page this is.

        Injected rather than copied into each application, because four copies
        of a navigation bar is four places to forget one.

        Not on a page a panel serves under its own prefix: a document being
        read wants to be a document, and the reader carries its own way back.
        """
        if not self.panels and not self.settings.open_channel:
            return page
        # The chat itself, and each panel's own page, get the switcher. Pages a
        # panel serves under a prefix of its own do not: a hearsay document
        # being read wants to be a document.
        #
        # This compared here.rstrip("/") against "/", and "/".rstrip("/") is
        # the empty string, so the chat failed its own test and lost the
        # switcher entirely.
        mine = {"/"} | {p.route for p in self.panels}
        if here not in mine and here.rstrip("/") + "/" not in mine:
            return page
        bar = hosting.nav_html(self.panels, here, self.settings.open_channel)
        marker = "<body>"
        if marker in page:
            return page.replace(marker, marker + bar, 1)
        return bar + page

    def publish(self, snapshot: dict) -> None:
        message = json.dumps(snapshot, separators=(",", ":"))
        with self._lock:
            if message == self._latest:
                return
            self._latest = message
            listeners = list(self._listeners)
        for channel in listeners:
            try:
                channel.put_nowait(message)
            except queue.Full:
                pass

    # -- setting up --------------------------------------------------------

    def note(self, text: str, role: str = "muted") -> None:
        self.log.append((time.time(), text, role))
        del self.log[:-200]

    def look_for_radio(self) -> None:
        """Ask every serial port whether it is a radio. Runs off the main
        thread because probing six ports takes a couple of seconds and a page
        that freezes looks broken."""
        self.busy = "looking for the module"
        self.publish(self.snapshot())

        def work():
          try:
            found = detect.find()
            self.ports = [{"port": f.port, "label": f.label, "answered": f.answered}
                          for f in found]
            self.busy = ""
            if not found:
                self.trouble = ("No serial ports at all. Is it plugged in? "
                                "Some cables carry power and no data.")
            else:
                # Silence is normal. Most of these modules are transparent and
                # answer nothing, so a quiet port is not a broken one.
                self.trouble = ""
            self.publish(self.snapshot())
          except Exception as exc:
            self.busy = ""
            self.trouble = f"Could not look at the serial ports: {exc}"
            self.publish(self.snapshot())

        threading.Thread(target=work, daemon=True).start()

    def begin(self, nick: str, passphrase: str, band: str, port: str) -> None:
        """Save what we were told, set the module up, and start."""
        if not (nick and passphrase and band in store.BANDS):
            self.trouble = "A name, a passphrase and a band, please."
            return
        self.settings = store.Settings(nick=nick.strip()[:16],
                                       passphrase=passphrase,
                                       band=band, port=port, configured=True)
        store.save(self.settings)
        self.trouble = ""
        threading.Thread(target=self._bring_up, daemon=True).start()

    def _bring_up(self) -> None:
        """Open the radio, apply the band, and start talking.

        Wrapped whole, because this runs off the main thread and a thread that
        dies quietly leaves somebody watching a page that says "setting the
        radio up" for ever with nothing to click.
        """
        try:
            self._bring_up_inner()
        except Exception as exc:
            self.busy = ""
            self.trouble = f"Could not start: {exc}"
            self.note(f"could not start: {exc}", "warn")
            self.publish(self.snapshot())

    def _bring_up_inner(self) -> None:
        self.busy = "setting the radio up"
        self.publish(self.snapshot())
        chosen = self.settings.port
        if not chosen:
            # Prefer something that already answered while we were looking, so
            # a second launch does not sit through the probe again.
            answered = [p["port"] for p in self.ports if p["answered"]]
            if len(answered) == 1:
                chosen = answered[0]
            else:
                found = detect.only_one()
                chosen = found.port if found else ""
        preset = self.settings.preset()
        config = RadioConfig(channel=preset["channel"], sf=preset["sf"],
                             power_dbm=preset["power"])
        self.identity = Identity.load_or_create(crypto.DEFAULT_PATH)
        keyring = Keyring(self.identity, self.settings.passphrase,
                          keystore=str(crypto.DEFAULT_PATH) + ".peers.json")
        # Beside the keypair, because that is what a face belongs to.
        self.faces.load(str(crypto.DEFAULT_PATH) + ".faces.json")
        bearers = []
        # A module that does not answer AT is still very probably the radio.
        #
        # These are transparent bridges: what you write goes out over the air,
        # so there is nothing on the other end that owes you an OK. Refusing
        # to open a port on that basis threw away a working radio, while the
        # command line client was happily talking on the same one.
        #
        # So the probe is a hint about which port to try first, and never a
        # reason not to try.
        quiet = bool(chosen) and not detect.answers(chosen)
        if chosen:
            try:
                self.busy = "telling the radio which band"
                self.publish(self.snapshot())
                radio = LoRaInterface(chosen, config,
                                      duty_limit_percent=preset["duty"])
                radio.open()
                radio.apply_config(verbose=False)
                bearers.append(radio)
                self.note(f"Radio ready on {chosen}, {preset['label']}.")
                if quiet:
                    self.note("It did not answer AT, which most of these do "
                              "not. If nobody appears, try the other port.")
            except Exception as exc:
                # This is the honest failure: the port would not open at all.
                self.trouble = (f"Could not open {chosen}: {exc}. "
                                f"Something else may be holding it, or it may "
                                f"be the wrong port.")
                self.busy = ""
                self.publish(self.snapshot())
                return
        elif not self.trouble:
            self.trouble = ("No radio found, so this will not reach anybody yet. "
                            "Plug one in and press start again.")
            self.note("Running without a radio.", "warn")
        self.link = Link(bearers, keyring=keyring)
        self.client = Client(self.link, self.identity, keyring,
                             nick=self.settings.nick)
        self.host = hosting.Host(client=self.client, identity=self.identity,
                                 panels=self.panels, faces=self.faces,
                                 _note=self.note, _set_face=self.set_my_face)
        for panel in self.panels:
            try:
                panel.start(self.host)
            except Exception as exc:
                self.note(f"{panel.title} would not start: {exc}", "warn")
        if self.panels:
            self.note("Also here: " + ", ".join(p.title for p in self.panels) + ".")
        self.client.session.set_face_mark(
            faces.mark(self.faces.own(self.identity.address)))
        self.busy = ""
        self.note(f"You are {self.settings.nick} ({self.identity.address}).")
        self.note(f"loraline {_version}, build {_build()}.")
        # Which way the window went, and why if it went the other way. This
        # was silent, so a bundle without a web view looked exactly like a
        # bundle with one that had decided not to bother.
        if own_window.showing:
            self.note("In a window of its own.", "muted")
        else:
            why = own_window.why_not() or "asked for the browser"
            self.note(f"In your browser: {why}", "muted")
        self.publish(self.snapshot())

    # -- running -----------------------------------------------------------

    def handle(self, order: dict) -> None:
        # Anything addressed to a panel goes straight there.
        which = order.get("panel")
        if which:
            for panel in self.panels:
                if panel.tag == which:
                    try:
                        panel.handle(order)
                    except Exception as exc:
                        self.note(f"{panel.title} stumbled: {exc}", "warn")
            return
        what = order.get("do")
        if what == "look":
            self.look_for_radio()
        elif what == "begin":
            self.begin(order.get("nick", ""), order.get("passphrase", ""),
                       order.get("band", ""), order.get("port", ""))
        elif what == "forget":
            self.settings = store.Settings()
            store.save(self.settings)
            self.note("Settings cleared. Start again.")
        elif what == "face" and self.client is not None:
            raw = order.get("bytes")
            if raw:
                try:
                    import base64
                    import io
                    self.set_my_face(faces.from_image(
                        io.BytesIO(base64.b64decode(raw))))
                except Exception as exc:
                    self.note(f"that picture would not go: {exc}", "warn")
        elif what == "unface" and self.client is not None:
            self.set_my_face("")
            self.note("Back to the picture your address had.")
        elif what == "announce" and self.client is not None:
            self.client.session.announce()
            self.note("Said hello. Anybody in range should see you now.")
        elif what == "checked" and self.client is not None:
            who = str(order.get("who") or "")
            self.client.keyring.check_off(who, bool(order.get("yes")))
            name = self.client.keyring.nicks.get(who) or who[:8]
            self.note(f"{name} is " + ("checked in person." if order.get("yes")
                                       else "no longer marked as checked."),
                      "gold" if order.get("yes") else "muted")
            self.publish(self.snapshot())
        elif what == "room":
            self.change_room(str(order.get("passphrase") or "").strip())
        elif what == "autostart":
            wanted = bool(order.get("on"))
            ok = self.autostart.turn_on() if wanted else self.autostart.turn_off()
            self.note("It will start with the computer." if wanted and ok else
                      "It will not start on its own." if ok else
                      "Could not change that; see the log.",
                      "muted" if ok else "warn")
        elif what == "convo":
            self.convo = order.get("convo") or GROUP
            self.unread.pop(self.convo, None)
        elif what == "say" and self.client is not None:
            text = (order.get("text") or "").strip()
            if text:
                for event in self.client.session.compose(text, self.convo,
                                                         time.time()):
                    self.absorb(event)
        elif what == "psm" and self.client is not None:
            said = str(order.get("text") or "").strip()[:40]
            self.client.session.set_psm(said)
            # Your own line shows nowhere but the box you typed it in, so
            # setting it looked exactly like nothing happening.
            self.note(f"Your line is now: {said}" if said
                      else "Your line is cleared.", "gold" if said else "muted")
            self.publish(self.snapshot())
        elif what == "status" and self.client is not None:
            try:
                picked = Status(order.get("status", "on"))
            except ValueError:
                return
            self.client.session.set_status(picked, time.time())
            self.note(f"You are {picked.name.lower()}.", "muted")
            self.publish(self.snapshot())

    def snapshot(self) -> dict:
        base = {
            "ready": self.client is not None,
            "busy": self.busy,
            "trouble": self.trouble,
            "ports": self.ports,
            "bands": [{"key": k, "label": v["label"]} for k, v in store.BANDS.items()],
            "settings": {"nick": self.settings.nick, "band": self.settings.band,
                         "port": self.settings.port,
                         "configured": self.settings.configured,
                         "open": self.settings.open_channel,
                         "open_phrase": store.OPEN_PHRASE},
            "log": [{"text": t, "role": r} for _, t, r in self.log[-60:]],
            "panels": [{"tag": p.tag, "title": p.title, "route": p.route,
                        "always": p.always} for p in self.panels],
            "radio": {
                "heard": getattr(getattr(self.client, "link", None),
                                 "frames_heard", 0),
                "foreign": getattr(getattr(self.client, "link", None),
                                   "frames_foreign", 0),
            },
            "version": f"{_version} \u00b7 {_build()}",
            "service": {
                "autostart": self.autostart.on,
                "how": self.autostart.describe(),
                # A panel that keeps working with nobody looking is the whole
                # reason this stays running, so say which ones those are.
                "carrying": [p.title for p in self.panels if p.always],
                "watchers": self.watchers,
            },
        }
        if self.client is None:
            return base
        session = self.client.session
        peers = []
        here = set()
        for peer in sorted(session.peers.values(), key=lambda p: p.label.lower()):
            here.add(peer.address)
            picture = self.faces.of(peer.address)
            peers.append({"address": peer.address,
                          "name": peer.nick or self.short(peer.address),
                          "status": peer.status.value, "psm": peer.psm,
                          "known": peer.known_key,
                          "checked": self.client.keyring.is_checked(peer.address),
                          "face": {"pixels": faces.unpack(picture),
                                   "colours": list(faces.colours_of(picture))},
                          "unread": self.unread.get(peer.address, 0)})

        # And the people you have checked, whether or not they are here.
        #
        # The keystore keeps everybody's keys and always will: forgetting one
        # is what lets somebody else take that address later. But keeping a
        # key and showing a name are different decisions. On the open channel
        # every stranger ever heard would otherwise pile up here for ever,
        # until somebody who talks to you weekly and somebody who passed
        # through in June looked the same.
        #
        # Reading sixteen characters out loud is effort, and nobody spends it
        # on a stranger, which makes it the right filter.
        ring = self.client.keyring
        for address in ring.remembered():
            if address in here or not ring.is_checked(address):
                continue
            picture = self.faces.of(address)
            peers.append({"address": address,
                          "name": ring.nicks.get(address) or self.short(address),
                          "status": "x", "psm": "",
                          "known": True,
                          "checked": ring.is_checked(address),
                          "away_since": ring.last_seen.get(address, 0.0),
                          "face": {"pixels": faces.unpack(picture),
                                   "colours": list(faces.colours_of(picture))},
                          "unread": self.unread.get(address, 0)})
        messages = [dict(item, state=self.state_of(item),
                         who=self.name_for(item.get("src"), item["who"]))
                    for item in self.threads.get(self.convo, [])[-80:]]
        base.update({
            "me": {"nick": session.nick, "address": self.identity.address,
                   "psm": session.psm, "status": session.status.value,
                   "own_face": bool(self.faces.mine),
                   "creature": faces.creature_of(self.identity.address),
                   "face": self.face_of(self.identity.address)},
            "peers": peers,
            "convo": self.convo,
            "unread_group": self.unread.get(GROUP, 0),
            "messages": messages,
            "airtime": self.airtime(),
        })
        for panel in self.panels:
            try:
                base[panel.tag] = panel.snapshot()
            except Exception as exc:
                # A panel that cannot describe itself used to hand the page an
                # empty object, and the page drew "undefined" everywhere with
                # nothing anywhere saying why.
                base[panel.tag] = {"broken": f"{type(exc).__name__}: {exc}"}
                if self._panel_trouble.get(panel.tag) != str(exc):
                    self._panel_trouble[panel.tag] = str(exc)
                    import traceback
                    traceback.print_exc()
                    self.note(f"{panel.title} could not draw itself: {exc}",
                              "warn")
        return base

    def absorb(self, event) -> None:
        from .session import AppEvent
        if isinstance(event, AppEvent):
            if event.app == faces.APP:
                return self.face_frame(event.src, event.payload)
            for panel in self.panels:
                if panel.tag == event.app:
                    try:
                        panel.heard(event.src, event.payload)
                    except Exception as exc:
                        self.note(f"{panel.title} stumbled: {exc}", "warn")
            return
        if isinstance(event, MessageEvent):
            thread = self.threads.setdefault(event.convo, [])
            line = {"who": event.who, "text": event.text,
                    "mine": not event.incoming, "seq": event.seq,
                    "when": int(event.when), "src": event.src}
            # The same message can arrive more than once: a sender retransmits
            # until it is acknowledged, and each attempt is sealed afresh, so
            # the link cannot tell them apart by their bytes. One person's
            # sequence number can.
            twice = any(old.get("src") == line["src"]
                        and old.get("seq") == line["seq"]
                        and old.get("who") == line["who"]
                        and line["seq"] >= 0
                        for old in thread)
            if twice:
                return
            thread.append(line)
            # Put it where it belongs rather than where it landed. Anything
            # said while you were out of range arrives when somebody comes
            # back, which is long after it was written, and a conversation
            # that reads in arrival order does not read at all.
            thread.sort(key=lambda item: item["when"])
            del thread[:-200]
            if event.incoming and event.convo != self.convo:
                self.unread[event.convo] = self.unread.get(event.convo, 0) + 1
        elif isinstance(event, SystemEvent):
            self.note(event.text, "warn" if event.level == "warn" else "muted")
        elif isinstance(event, PresenceEvent):
            # Nothing to say and nothing to read: it is an empty marker
            # meaning the roster changed, and the roster is in the snapshot
            # that goes out at the end of the turn.
            #
            # This read event.text, which a PresenceEvent has never had, so
            # every single presence raised and took the turn down with it
            # before the snapshot was published. The peer was in the session
            # the whole time; the page was never told.
            pass

    def change_room(self, passphrase: str) -> None:
        """Move to a different conversation without becoming a different person.

        Your keypair, your name and your picture are yours; the passphrase is
        only which conversation you can hear. Changing it used to mean finding
        settings.json and deleting it, which is not a thing to ask of anybody.
        """
        if not passphrase:
            return self.note("A room needs a phrase.", "warn")
        if passphrase == self.settings.passphrase:
            return self.note("You are already in that one.")
        self.settings.passphrase = passphrase
        store.save(self.settings)
        if self.client is not None:
            self.unread.clear()
            self.convo = ""
            self.client.rekey(passphrase)
            # Say hello on the new channel at once. Waiting for the next
            # heartbeat means a minute of looking like nobody is there, which
            # is indistinguishable from it not working.
            self.client.session.announce()
        self.note("Moved. The people here are whoever has the same phrase.",
                  "gold" if not self.settings.open_channel else "warn")
        self.publish(self.snapshot())

    # -- faces -------------------------------------------------------------

    def face_frame(self, src: str, payload: str) -> None:
        """Somebody asking for a picture, or sending one."""
        if payload.startswith("?"):
            wanted = payload[1:]
            mine = self.faces.own(self.identity.address)
            if wanted and wanted != faces.mark(mine):
                return          # they are after a picture we no longer have
            for piece in faces.offer(mine):
                self.client.session.send_app(faces.APP, piece)
            return
        collecting = self.arriving.setdefault(src, faces.Arriving())
        whole = collecting.take(payload)
        if whole is None:
            return
        self.arriving.pop(src, None)
        self.asked.pop(src, None)
        if self.faces.learn(src, whole):
            peer = self.client.session.peers.get(src)
            self.note(f"{peer.label if peer else src} has a face now.")

    def want_faces(self, now: float) -> None:
        """Ask anybody whose picture we do not have for it.

        Only for people we can hear, only once a minute, and only when their
        mark says they have something we lack. A face is set once and then
        almost never, so this is quiet after the first minute of knowing
        somebody.
        """
        if self.client is None:
            return
        for peer in self.client.session.online_peers():
            if not peer.face_mark:
                continue
            if faces.mark(self.faces.of(peer.address, fallback=False)) == peer.face_mark:
                continue
            if now - self.asked.get(peer.address, 0.0) < 60.0:
                continue
            self.asked[peer.address] = now
            self.client.session.send_app(faces.APP, faces.ask(peer.face_mark))

    def set_my_face(self, picture: str) -> None:
        self.faces.set_mine(picture)
        self.client.session.set_face_mark(faces.mark(picture))
        self.note("Your picture is set. People near you will pick it up.")

    def name_for(self, address: str, fallback: str = "") -> str:
        """What to call somebody, worked out now rather than when they spoke.

        A message used to keep whatever name was known the moment it arrived,
        so anything said before their nick turned up stayed addressed to
        sixteen hex characters for ever.
        """
        if not address:
            return fallback
        if self.client is not None:
            peer = self.client.session.peers.get(address)
            if peer is not None and peer.nick:
                return peer.nick
            known = self.client.keyring.nicks.get(address)
            if known:
                return known
        return fallback or self.short(address)

    @staticmethod
    def short(address: str) -> str:
        """Enough of an address to tell people apart, in a narrow column."""
        return address[:8] + "\u2026" if len(address) > 9 else address

    def state_of(self, item) -> str:
        if item["mine"] and self.client is not None:
            out = self.client.session.outgoing(item["seq"])
            return out.state.value if out is not None else ""
        return ""

    def face_of(self, address: str) -> dict:
        picture = (self.faces.own(address) if address == self.identity.address
                   else self.faces.of(address))
        return {"pixels": faces.unpack(picture),
                "colours": list(faces.colours_of(picture))}

    def airtime(self) -> str:
        for bearer in self.link.interfaces if self.link else []:
            budget = getattr(bearer, "budget", None)
            if budget is not None:
                return f"{budget.remaining_ms(time.time())/1000:.0f} s"
        return ""

    def prepare(self) -> bool:
        """Get the server and the radio going. False if somebody beat us to it."""
        # One at a time. Two copies would fight over the serial port and the
        # loser would look broken rather than second, so the second one just
        # opens a window onto the first and gets out of the way.
        if not service.only_one(self.port):
            print("loraline is already running; opening its window.", flush=True)
            service.raise_window(self.port)
            self.running = False
            return False
        self.start_server()
        if self.settings.ready:
            threading.Thread(target=self._bring_up, daemon=True).start()
        else:
            self.look_for_radio()
        return True

    def run(self) -> None:
        if not self.prepare():
            return
        # BROWSER=echo is a Unix convention and means nothing on macOS or
        # Windows, where this would try to open a real browser on a machine
        # that has none. A build server is exactly that machine.
        if not os.environ.get("LORALINE_NO_BROWSER"):
            try:
                webbrowser.open(f"http://127.0.0.1:{self.port}")
            except Exception:
                pass
        print(f"loraline is at http://127.0.0.1:{self.port}")
        self.loop()

    def loop(self) -> None:
        """Turn until told to stop."""
        while self.running:
            # One bad tick must not take the node down with it.
            #
            # Nothing here was guarded, so any exception anywhere ended the
            # loop, run() returned, the process exited and the window went to
            # connection refused. A radio that stops holding other people's
            # messages because one frame was malformed is worse than a radio
            # that says what went wrong and carries on.
            try:
                self.one_turn()
            except Exception as exc:
                self.stumbled(exc)
            time.sleep(0.2)

    def one_turn(self) -> None:
        now = time.time()
        if self.client is not None:
            for event in self.client.pump():
                self.absorb(event)
            for peer in self.client.session.online_peers():
                self.client.keyring.saw(peer.address, now)
            self.want_faces(now)
            for panel in self.panels:
                # A panel that is always on keeps working whatever is on
                # screen; that is the whole difference between a game and
                # something holding other people's messages.
                try:
                    panel.tick(now)
                except Exception as exc:
                    self.note(f"{panel.title} stumbled: {exc}", "warn")
        for order in self.drain():
            self.handle(order)
        self.publish(self.snapshot())

    def stumbled(self, exc: Exception) -> None:
        """Say what went wrong, once per kind, and keep going."""
        import traceback
        kind = f"{type(exc).__name__}: {exc}"
        if self._last_stumble != kind:
            self._last_stumble = kind
            traceback.print_exc()
            self.note(f"Something went wrong and was skipped: {kind}", "warn")
            try:
                self.publish(self.snapshot())
            except Exception:
                pass

    def drain(self) -> list:
        out = []
        while True:
            try:
                out.append(self.inbox.get_nowait())
            except queue.Empty:
                return out


def main(argv=None) -> int:
    app = App()
    # A window of its own if this machine has one, and the browser if not.
    #
    # The web view has to own the main thread on macOS, so the node turns on a
    # thread beside it rather than the other way round. Everything else is the
    # same program serving the same page.
    if own_window.wanted() and own_window.available():
        if app.prepare():
            url = f"http://127.0.0.1:{app.port}"
            print(f"loraline is at {url}")
            if own_window.show(url, "loraline", serve=app.loop):
                app.running = False
                return 0
        else:
            return 0
    trouble = own_window.why_not()
    if trouble and os.environ.get("LORALINE_WINDOW", "").strip() == "1":
        print(trouble, file=sys.stderr)
    app.run()
    return 0


def _sorry(title: str, exc: Exception) -> str:
    """Say which panel broke and how, instead of quietly showing another."""
    from html import escape
    return ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{escape(title)}</title><style>body{{margin:0;background:#f7f5f0;"
            "color:#22201d;font:17px/1.6 ui-serif,Charter,Georgia,serif}"
            ".p{max-width:34rem;margin:3rem auto;padding:0 1.2rem}"
            "code{font:13px ui-monospace,Menlo,monospace;background:#eee;"
            "padding:.1rem .3rem}</style></head><body><div class=\"p\">"
            f"<h1>{escape(title)} could not draw its page</h1>"
            f"<p><code>{escape(type(exc).__name__)}: {escape(str(exc))}</code></p>"
            "<p>The rest of loraline is still running. This is a fault worth "
            "reporting.</p></div></body></html>")


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>loraline</title>
<style>
:root{--paper:#f7f5f0;--panel:#eeebe3;--ink:#22201d;--soft:#5d5952;--faint:#8d8880;
      --rule:#dcd7cc;--mark:#b01b62;--good:#2f7d4f;
      --serif:ui-serif,Charter,Georgia,serif;
      --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--serif);
     font-size:16px;line-height:1.5}
.wrap{max-width:52rem;margin:0 auto;padding:0 1rem 2rem;min-height:100vh;
  width:100%;
      display:flex;flex-direction:column}
header{display:flex;align-items:baseline;justify-content:space-between;gap:1rem;
  flex-wrap:wrap;
       padding:1rem 0 .7rem;border-bottom:1px solid var(--rule)}
h1{font-size:1.1rem;margin:0;font-weight:600}
h1 span{color:var(--mark);font-family:var(--mono)}
.meta{font-family:var(--mono);font-size:.72rem;color:var(--faint);text-align:right;
  overflow-wrap:anywhere}
/* setting up */
.setup{max-width:27rem;margin:2.4rem auto;width:100%}
.setup h2{font-size:1.5rem;margin:0 0 .3rem;font-weight:600}
.setup p.lead{color:var(--soft);margin:0 0 1.6rem}
label{display:block;font-size:.8rem;color:var(--soft);margin:.9rem 0 .25rem}
input,select,textarea,button{font:inherit}
input[type=text],input[type=password],select{width:100%;background:#fff;
  border:1px solid var(--rule);color:var(--ink);padding:.55rem .6rem;border-radius:3px}
input:focus,select:focus{outline:2px solid var(--mark);outline-offset:1px}
.hint{font-size:.78rem;color:var(--faint);margin:.3rem 0 0}
button{background:var(--panel);border:1px solid var(--rule);color:var(--ink);
       padding:.55rem 1rem;border-radius:3px;cursor:pointer}
button:hover{background:#e4e0d5}
button.go{background:var(--mark);border-color:var(--mark);color:#fff;margin-top:1.4rem}
button.go:hover{background:#951552}
button.small{padding:.3rem .6rem;font-size:.8rem}
.trouble{background:#fbeaef;border-left:2px solid var(--mark);padding:.6rem .8rem;
         font-size:.88rem;margin:1rem 0 0}
.busy{color:var(--soft);font-size:.88rem;margin:1rem 0 0}
/* talking */
.cols{display:flex;gap:1rem;flex:1;min-height:0;padding-top:.8rem}
.side{width:12rem;flex:none;font-size:.88rem}
.side h3{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;
         color:var(--faint);margin:.9rem 0 .3rem;font-weight:600}
.who{display:block;width:100%;text-align:left;background:none;border:0;
     padding:.3rem .4rem;border-radius:3px;cursor:pointer;line-height:1.3}
.who:hover{background:var(--panel)}
.who[aria-current]{background:var(--panel);font-weight:600}
.dot{display:inline-block;width:.5em;height:.5em;border-radius:50%;margin-right:.4em}
.o{background:var(--good)}.a{background:#c9a227}.b{background:#c05c2f}.x{background:#bdb8ae}
.psm{display:block;font-size:.74rem;color:var(--faint)}
.pip{float:right;background:var(--mark);color:#fff;border-radius:8px;
     font-size:.66rem;padding:0 .35rem;font-family:var(--mono)}
.talk{flex:1;display:flex;flex-direction:column;min-width:0}
.lines{flex:1;overflow-y:auto;padding:.2rem .2rem .6rem}
.line{margin:0 0 .45rem;overflow-wrap:anywhere}
.line b{font-weight:600}
.line .addr{font-family:var(--mono);font-size:.7rem;color:var(--faint)}
.line.mine b{color:var(--mark)}
.tick{font-family:var(--mono);font-size:.7rem;color:var(--faint);margin-left:.3rem;
  cursor:default}
.tick.delivered{color:var(--mark)}
.tick.failed{color:#b4472f}
.tick.d{color:var(--good)}.tick.f{color:var(--mark)}
form.say{display:flex;gap:.5rem;padding-top:.5rem;border-top:1px solid var(--rule)}
/* A flex item will not shrink below its content unless told to, which is how
   an input box pushes the send button off a narrow window. */
.say input{flex:1;min-width:0}
.say button{flex:none}
form.say input{flex:1}
.log{font-family:var(--mono);font-size:.7rem;color:var(--faint);
     max-height:5.5rem;overflow-y:auto;border-top:1px solid var(--rule);
     margin-top:.6rem;padding-top:.4rem}
.log .warn{color:var(--mark)}
.staying{font-size:.76rem;color:var(--faint);line-height:1.45;margin:.2rem 0 .6rem}
.staying b{color:var(--ink);font-weight:600}
.staying label{display:flex;gap:.4rem;align-items:flex-start;margin-top:.4rem;
  cursor:pointer;color:var(--soft)}
.staying input{margin:.18rem 0 0}
.saying{margin:.35rem 0 .2rem;display:flex;gap:.4rem;align-items:baseline;
  flex-wrap:wrap}
.saying .said{color:var(--ink);font-size:.86rem}
.saying input{flex:1;min-width:0;background:var(--panel);border:1px solid var(--rule);
  color:var(--ink);font:inherit;font-size:.82rem;padding:.25rem .4rem;border-radius:2px}
.states{margin:0 0 .6rem;display:flex;flex-wrap:wrap;gap:.3rem}
button.state{background:none;border:1px solid transparent;color:var(--faint);
  font:inherit;font-size:.74rem;padding:.1rem .35rem;border-radius:2px;cursor:pointer}
button.state:hover{color:var(--ink)}
button.state.is{border-color:var(--rule);background:var(--panel);color:var(--ink)}
.hereis{font-size:.78rem;color:var(--faint);line-height:1.5;margin:.2rem 0 .5rem}
.verify{font-size:.76rem;color:var(--faint);line-height:1.5;margin:.1rem 0 .6rem;
  border-left:2px solid var(--rule);padding-left:.6rem}
.verify b{font-family:var(--mono);color:var(--soft);font-size:.92em}
.verify.done{border-left-color:var(--mark);color:var(--soft)}
#roomform{margin:.2rem 0 .7rem}
#roomform input{width:100%;background:var(--panel);border:1px solid var(--rule);
  color:var(--ink);font:inherit;font-size:.82rem;padding:.3rem .4rem;border-radius:2px}
.roomrow{display:flex;gap:.5rem;align-items:baseline;margin-top:.35rem}
.roomrow button:first-child{background:var(--panel);border:1px solid var(--rule);
  color:var(--ink);font:inherit;font-size:.8rem;padding:.2rem .6rem;
  border-radius:2px;cursor:pointer}
button.plain{background:none;border:0;padding:0;font:inherit;color:var(--mark);
  text-decoration:underline;cursor:pointer}
canvas.face{width:34px;height:34px;image-rendering:pixelated;border:1px solid var(--rule);
  border-radius:2px;background:var(--panel);flex:none}
.who{display:flex;gap:.5rem;align-items:center}
.who[data-away="1"]{opacity:.5}
.checked{color:var(--mark);font-size:.72rem;margin-left:.2rem}
.who canvas.face{width:26px;height:26px}
.whotext{min-width:0;flex:1}
.mine{display:flex;gap:.7rem;align-items:center;margin:.2rem 0 .6rem}
.mine .lines{font-size:.76rem;color:var(--faint);line-height:1.45}
.mine label{color:var(--soft);text-decoration:underline;
  text-decoration-color:var(--mark);cursor:pointer}
.mine input[type=file]{display:none}
.mine button.plain{background:none;border:0;color:var(--faint);padding:0;
  font-size:.76rem;text-decoration:underline;cursor:pointer}
/* Stack sooner than the columns strictly break, because a twelve rem sidebar
   beside a squeezed conversation is worse than one above it. */
@media(max-width:46rem){
  .cols{flex-direction:column}
  .side{width:auto}
  .meta{text-align:left}
}
@media(max-width:30rem){
  .wrap{padding:0 .6rem 2rem}
  .side{font-size:.84rem}
}
</style></head><body>
<div class="wrap">
<header>
  <h1><svg class="mark" viewBox="0 0 32 32" aria-hidden="true"><circle cx="9.4" cy="12.1" r="2.3" fill="currentColor"/><circle cx="22.6" cy="12.1" r="2.3" fill="currentColor"/><path d="M8.5 18.1 A 8.5 4.9 0 0 0 23.5 18.1" fill="none" stroke="#b01b62" stroke-width="2.1" stroke-linecap="round"/></svg> loraline</h1>
  <div class="meta" id="meta"></div>
</header>
<div id="body"></div>
</div>
<script>
let state = {}, started = false;
const esc = s => (s||'').replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
const send = o => fetch('/', {method:'POST', body: JSON.stringify(o)});

function setup(){
  const s = state.settings || {}, ports = state.ports || [];
  const answered = ports.filter(p => p.answered);
  return `
  <div class="setup">
    <h2>Let's get you on the air</h2>
    <p class="lead">Three things, once. After this it just starts.</p>

    <label for="nick">What should people call you</label>
    <input type="text" id="nick" maxlength="16" value="${esc(s.nick)}" placeholder="hank">

    <label for="pass">The passphrase your group agreed</label>
    <input type="password" id="pass" placeholder="the same words everybody else typed">
    <p class="hint">Not a password for an account; there are no accounts. It is the
      key to the conversation, and everybody needs the same one. Say it out loud to
      each other rather than sending it.</p>

    <label for="band">Where you are</label>
    <select id="band">
      ${(state.bands||[]).map(b =>
        `<option value="${b.key}" ${b.key===s.band?'selected':''}>${esc(b.label)}</option>`).join('')}
    </select>
    <p class="hint">This decides the frequency you transmit on, which is a legal
      matter and not a preference. Pick where you actually are.</p>

    <label for="port">The module</label>
    <select id="port">
      ${ports.length
        ? ports.map(p => `<option value="${esc(p.port)}" ${p.answered?'selected':''}>${
            esc(p.label)}${p.answered?' \u2014 answered':''}</option>`).join('')
        : '<option value="">nothing found</option>'}
    </select>
    <p class="hint">
      ${ports.length === 1 ? 'One found.' :
        ports.length > 1 ? 'More than one. If the first does not work, try the other.' :
        'Nothing found yet.'}
      <button class="small" type="button" onclick="send({do:'look'})">look again</button>
    </p>

    ${state.busy ? `<p class="busy">${esc(state.busy)}\u2026</p>` : ''}
    ${state.trouble ? `<p class="trouble">${esc(state.trouble)}</p>` : ''}

    <button class="go" type="button" onclick="begin()">Start</button>
  </div>`;
}

const STATUS = {on:'online', away:'away', busy:'busy', brb:'back in a bit'};

/* The box only appears while you are changing it. A field sitting there for
   ever, with the line you set showing nowhere, made setting it look exactly
   like nothing happening. */
let sayingOpen = false;

function editSaying(){
  sayingOpen = true;
  render();
  const box = document.getElementById('psm');
  if(box){ box.focus(); box.select(); }
}

function savePsm(){
  const box = document.getElementById('psm');
  if(box) send({do:'psm', text: box.value.trim()});
  sayingOpen = false;
}

function showRoom(){
  roomOpen = !roomOpen;
  const box = document.getElementById('roomform');
  if(box) box.style.display = roomOpen ? 'block' : 'none';
  if(roomOpen) document.getElementById('newpass').focus();
}

function joinRoom(){
  const box = document.getElementById('newpass');
  if(!box.value.trim()) return;
  send({do:'room', passphrase: box.value.trim()});
  box.value = '';
  roomOpen = false;
}

function joinOpen(){
  send({do:'room', passphrase: (state.settings||{}).open_phrase || ''});
  roomOpen = false;
}

function openChannel(){
  const box = document.getElementById('pass');
  box.value = (state.settings || {}).open_phrase || '';
  box.type = 'text';                 // it is not a secret, so do not hide it
}

function begin(){
  send({do:'begin',
        nick: document.getElementById('nick').value,
        passphrase: document.getElementById('pass').value,
        band: document.getElementById('band').value,
        port: document.getElementById('port').value});
}

/* Keyed on what the session actually reports. It was keyed on single letters
   while the states are whole words, so the lookup missed every time and the
   app has never shown a tick. */
const TICK = {
  queued:    ['\u00b7',        'waiting for the radio'],
  sent:      ['\u00b7',        'went out, nobody has confirmed yet'],
  partial:   ['\u2713',        'some of them have it'],
  delivered: ['\u2713\u2713',  'arrived'],
  failed:    ['\u2717',        'lost, and nobody got it'],
};

function staying(){
  const s = state.service || {};
  const carrying = (s.carrying || []);
  return `
    ${carrying.length
      ? `<b>${carrying.join(' and ')}</b> keeps working when you close this
         window. Closing it does not quit.`
      : `Closing this window does not quit. The radio stays open so people can
         still reach you.`}
    <label><input type="checkbox" id="auto" ${s.autostart ? 'checked' : ''}
      onchange="send({do:'autostart', on:this.checked})">
      <span>start with the computer${s.autostart
        ? `<br><span class="hint">${esc(s.how || '')}</span>` : ''}</span></label>`;
}

/* What the radio has actually heard.
 *
 * Three different faults look identical from the outside and this tells them
 * apart without a log file: nothing heard at all is the radios not reaching
 * each other, frames heard but none decoding is the wrong passphrase, and
 * frames decoding with nobody appearing is a fault in here.
 */
function radioLine(){
  const r = state.radio || {};
  if(!r.heard) return '<br><span class="hint">Nothing heard on the radio yet.</span>';
  const mine = r.heard - (r.foreign||0);
  if(!mine) return `<br><span class="hint">Heard ${r.heard} transmission(s), none
    of them for this room. Somebody is out there on a different passphrase.</span>`;
  return `<br><span class="hint">Heard ${r.heard}, ${mine} for this room.</span>`;
}

function verifyStrip(){
  const who = (state.peers||[]).find(p => p.address === state.convo);
  if(!who) return '';
  /* Keys are trusted on first contact, so a stranger who got there before the
     real person is indistinguishable from them. Reading the address out loud
     is the only thing that settles it, and only the person at the screen can
     do it. */
  return who.checked
    ? `<p class="verify done">&#x2713; you checked <b>${esc(who.address)}</b> with
       them in person.
       <button class="plain" type="button"
         onclick="send({do:'checked', who:'${who.address}', yes:false})">undo</button></p>`
    : `<p class="verify">Read <b>${esc(who.address)}</b> out to them. If it
       matches what their screen says,
       <button class="plain" type="button"
         onclick="send({do:'checked', who:'${who.address}', yes:true})">tick it
       off</button>.</p>`;
}

function talking(){
  const me = state.me || {}, peers = state.peers || [];
  /* Everybody met, not just everybody in range. Somebody you talked to
     yesterday used to vanish completely when they were not about. */
  const people = peers;
  return `
  <div class="cols">
    <div class="side">
      <h3>you</h3>
      <div class="mine">
        <canvas class="face" id="myface" width="32" height="32"></canvas>
        <div class="lines">
          <label for="pick">use a photograph</label>
          <input type="file" id="pick" accept="image/*">
          ${(state.me||{}).own_face
            ? '<br><button class="plain" type="button" onclick="send({do:\'unface\'})">back to the ' + esc((state.me||{}).creature||'default') + '</button>'
            : '<br>you are the ' + esc((state.me||{}).creature||'default')}
        </div>
      </div>
      <p class="saying">${sayingOpen
        ? `<input id="psm" maxlength="40" placeholder="say what you are up to"
                 value="${esc(me.psm || '')}"
                 onkeydown="if(event.key==='Enter')savePsm()">
           <button class="plain" type="button" onclick="savePsm()">set</button>`
        : (me.psm
            ? `<span class="said">${esc(me.psm)}</span>
               <button class="plain" type="button" onclick="editSaying()">change</button>`
            : `<button class="plain" type="button" onclick="editSaying()">say what you
               are up to</button>`)}</p>
      <p class="states">
        ${['on','away','busy','brb'].map(s => `<button type="button"
           class="state ${me.status===s?'is':''}"
           onclick="send({do:'status', status:'${s}'})">${STATUS[s]}</button>`).join('')}
      </p>

      <h3>room</h3>
      <p class="staying">
        ${(state.settings||{}).open ? 'You are on the <b>open channel</b>.'
          : 'You are in a private room.'}
        <br><button class="plain" type="button" onclick="showRoom()">change room</button>
      </p>
      <div id="roomform" style="display:${roomOpen ? 'block' : 'none'}">
        <input id="newpass" type="password" placeholder="a different passphrase">
        <div class="roomrow">
          <button type="button" onclick="joinRoom()">join</button>
          <button class="plain" type="button" onclick="joinOpen()">the open one</button>
        </div>
        <p class="hint">Everyone in a room needs the same phrase. Your name,
          your picture and your keys stay as they are.</p>
      </div>
      <h3>this window</h3>
      <p class="staying"><span class="hint">loraline ${esc(state.version||'?')}</span></p>
      <p class="staying">${staying()}</p>
      <h3>conversations</h3>
      <button class="who" ${state.convo==='*'?'aria-current="page"':''}
        onclick="send({do:'convo',convo:'*'})">
        ${state.unread_group ? `<span class="pip">${state.unread_group}</span>` : ''}
        everybody</button>
      <h3>in range</h3>
      <p class="hereis">
        <button class="plain" type="button" onclick="send({do:'announce'})">say
        you are here</button>
        ${radioLine()}
      </p>
      ${people.length ? people.map((p,i) => `
        <button class="who" data-away="${p.status==='x'?1:0}"
          ${state.convo===p.address?'aria-current="page"':''}
          onclick="send({do:'convo',convo:'${p.address}'})">
          <canvas class="face" data-face="${i}" width="32" height="32"></canvas>
          <span class="whotext">
            ${p.unread ? `<span class="pip">${p.unread}</span>` : ''}
            <span class="dot ${p.status}"></span>${esc(p.name)}
            ${p.checked ? `<span class="checked" title="you compared this address with them in person">&#x2713;</span>` : ''}
            ${p.psm ? `<span class="psm">${esc(p.psm)}</span>`
                    : (p.status === 'x' && p.away_since
                        ? `<span class="psm">${since(p.away_since)}</span>` : '')}
          </span>
        </button>`).join('') : '<p class="hint">Nobody yet. They appear on their own.</p>'}
    </div>
    <div class="talk">
      ${state.convo ? verifyStrip() : ''}
      <div class="lines" id="lines">
        ${(state.messages||[]).map(m => `
          <p class="line ${m.mine?'mine':''}"><b>${esc(m.who)}</b>
            ${m.text ? esc(m.text) : ''}
            ${m.mine && TICK[m.state] ? `<span class="tick ${m.state}"
              title="${TICK[m.state][1]}">${TICK[m.state][0]}</span>` : ''}
          </p>`).join('') ||
          '<p class="hint">Nothing said yet. Anything you send waits until somebody is in range.</p>'}
      </div>
      <form class="say" onsubmit="say(event)">
        <input type="text" id="text" maxlength="400" placeholder="say something">
        <button>send</button>
      </form>
      <div class="log">${(state.log||[]).slice(-14).reverse()
        .map(l => `<div class="${l.role}">${esc(l.text)}</div>`).join('')}</div>
    </div>
  </div>`;
}

/* Faces arrive as a palette and a pixel a byte, which is what the radio
   carried. Drawing one is a loop, not a library. */
/* Roughly when somebody was last heard.
 *
 * Deliberately vague. A count of minutes is the WhatsApp habit and it makes a
 * contact list into an attendance record: forty minutes ago invites "you were
 * about, why did you not answer", and earlier today does not.
 *
 * It would also be lying. Everybody's heartbeat slows as more people arrive,
 * so at twenty people somebody is heard every five minutes and counted away
 * after thirteen, and a minute count is precision the radio never had.
 */
function since(when){
  if(!when) return 'met before';
  const now = new Date(), then = new Date(when * 1000);
  const secs = Math.max(0, now/1000 - when);
  const day = d => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const days = Math.round((day(now) - day(then)) / 86400000);
  /* Nothing at all for today or yesterday. Whether somebody was about this
     morning is nobody's business, and this is a list of people you know
     rather than a register of who turned up. */
  if(days <= 1) return '';
  if(days < 7) return 'a few days ago';
  if(days < 60) return 'a while ago';
  return 'a long time ago';
}

function paint(canvas, face){
  if(!canvas || !face || !face.pixels || !face.pixels.length) return;
  const g = canvas.getContext('2d');
  const n = Math.round(Math.sqrt(face.pixels.length));
  canvas.width = n; canvas.height = n;
  const img = g.createImageData(n, n);
  for(let i = 0; i < face.pixels.length; i++){
    const hex = face.colours[face.pixels[i]] || '#000000';
    img.data[i*4]   = parseInt(hex.slice(1,3),16);
    img.data[i*4+1] = parseInt(hex.slice(3,5),16);
    img.data[i*4+2] = parseInt(hex.slice(5,7),16);
    img.data[i*4+3] = 255;
  }
  g.putImageData(img, 0, 0);
}

function paintAll(){
  if(state.me) paint(document.getElementById('myface'), state.me.face);
  (state.peers||[]).forEach((p,i) =>
    paint(document.querySelector(`canvas[data-face="${i}"]`), p.face));
  const pick = document.getElementById('pick');
  if(pick && !pick.dataset.wired){
    pick.dataset.wired = '1';
    pick.onchange = ev => {
      const file = ev.target.files[0];
      if(!file) return;
      /* The node turns it into thirty-two pixels: one place, one way, so
         everybody's face is made the same. This only shrinks it first, so a
         twelve megapixel photograph is not pushed at something that will keep
         a thousand pixels of it. */
      const reader = new FileReader();
      reader.onload = () => {
        const img = new Image();
        img.onload = () => {
          const scale = Math.min(1, 256 / Math.max(img.width, img.height));
          const c = document.createElement('canvas');
          c.width = Math.max(1, Math.round(img.width*scale));
          c.height = Math.max(1, Math.round(img.height*scale));
          c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
          send({do:'face', bytes: c.toDataURL('image/png').split(',')[1]});
        };
        img.src = reader.result;
      };
      reader.readAsDataURL(file);
      ev.target.value = '';
    };
  }
}

function say(e){
  e.preventDefault();
  const box = document.getElementById('text');
  if(box.value.trim()) send({do:'say', text: box.value.trim()});
  box.value = '';
}

/* The airtime counter changes every second, so the snapshot changes, so the
   page is rebuilt. Anything the person was in the middle of goes with it: a
   half-typed message, an opened form. These survive the rebuild. */
let roomOpen = false;

function keepTyping(fn){
  const before = {};
  for(const box of document.querySelectorAll('input[type=text], input:not([type]), textarea, input[type=password]'))
    if(box.id) before[box.id] = [box.value, box === document.activeElement,
                                 box.selectionStart, box.selectionEnd];
  fn();
  for(const [id, [value, focused, from, to]] of Object.entries(before)){
    const box = document.getElementById(id);
    if(!box) continue;
    box.value = value;
    if(focused){
      box.focus();
      try { box.setSelectionRange(from, to); } catch(e) {}
    }
  }
}

function render(){
  const meta = document.getElementById('meta');
  if(state.ready && state.me){
    meta.innerHTML = `${esc(state.me.nick)} (${state.me.address})` +
      (state.airtime ? `<br>${state.airtime} of the hour left` : '');
  } else {
    meta.textContent = state.busy || '';
  }
  const want = state.ready ? 'talk' : 'setup';
  const body = document.getElementById('body');
  if(body.dataset.view !== want || want === 'talk' || state.busy || state.trouble){
    const keep = document.activeElement && document.activeElement.id;
    const values = {};
    ['nick','pass','band','port','text'].forEach(id => {
      const el = document.getElementById(id); if(el) values[id] = el.value;
    });
    keepTyping(() => { body.innerHTML = state.ready ? talking() : setup(); });
    body.dataset.view = want;
    Object.entries(values).forEach(([id, v]) => {
      const el = document.getElementById(id);
      if(el && v && !el.value) el.value = v;
    });
    if(keep){ const el = document.getElementById(keep); if(el) el.focus(); }
    const lines = document.getElementById('lines');
    if(lines) lines.scrollTop = lines.scrollHeight;
    paintAll();
  }
}

new EventSource('/events').onmessage = m => { state = JSON.parse(m.data); render(); };
</script></body></html>
"""
