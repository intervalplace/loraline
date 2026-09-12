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
import queue
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import crypto, detect, host as hosting, service, settings as store
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
            if panel.route and route.rstrip("/") == panel.route.rstrip("/"):
                try:
                    body = panel.page()
                except Exception:
                    body = PAGE
                break
        return self.with_nav(body, route)

    def with_nav(self, page: str, here: str) -> str:
        """Put the switcher at the top of whatever page this is.

        Injected rather than copied into each application, because four copies
        of a navigation bar is four places to forget one.
        """
        if not self.panels:
            return page
        bar = hosting.nav_html(self.panels, here)
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
                                "Some cables are power only.")
            elif not any(f.answered for f in found):
                self.trouble = ("Found a port but nothing answered. It may need "
                                "its driver, or it may be a different device.")
            else:
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
        bearers = []
        if chosen and not detect.answers(chosen):
            # Applying a band to something that is not a radio takes nine
            # seconds of sending AT into the dark and then reports success,
            # which is worse than failing. Say so, and carry on without it:
            # refusing to start leaves somebody with a blank window and no way
            # to change their mind.
            self.trouble = (f"{chosen} did not answer, so this is not on the air. "
                            f"It may be the wrong port, or the module may need "
                            f"its driver. Everything else still works.")
            self.note(f"{chosen} did not answer; carrying on without a radio", "warn")
            chosen = ""
        if chosen:
            try:
                self.busy = "telling the radio which band"
                self.publish(self.snapshot())
                radio = LoRaInterface(chosen, config,
                                      duty_limit_percent=preset["duty"] * 100)
                radio.open()
                radio.apply_config(verbose=False)
                bearers.append(radio)
                self.note(f"Radio ready on {chosen}, {preset['label']}.")
            except Exception as exc:
                self.trouble = f"Could not open {chosen}: {exc}"
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
                                 panels=self.panels, _note=self.note)
        for panel in self.panels:
            try:
                panel.start(self.host)
            except Exception as exc:
                self.note(f"{panel.title} would not start: {exc}", "warn")
        if self.panels:
            self.note("Also here: " + ", ".join(p.title for p in self.panels) + ".")
        self.busy = ""
        self.note(f"You are {self.settings.nick} ({self.identity.address}).")
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
            self.client.session.set_psm(order.get("text", ""))
        elif what == "status" and self.client is not None:
            try:
                self.client.session.set_status(Status(order.get("status", "o")),
                                               time.time())
            except ValueError:
                pass

    def snapshot(self) -> dict:
        base = {
            "ready": self.client is not None,
            "busy": self.busy,
            "trouble": self.trouble,
            "ports": self.ports,
            "bands": [{"key": k, "label": v["label"]} for k, v in store.BANDS.items()],
            "settings": {"nick": self.settings.nick, "band": self.settings.band,
                         "port": self.settings.port,
                         "configured": self.settings.configured},
            "log": [{"text": t, "role": r} for _, t, r in self.log[-60:]],
            "panels": [{"tag": p.tag, "title": p.title, "route": p.route,
                        "always": p.always} for p in self.panels],
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
        for peer in sorted(session.peers.values(), key=lambda p: p.label.lower()):
            peers.append({"address": peer.address, "name": peer.label,
                          "status": peer.status.value, "psm": peer.psm,
                          "known": peer.known_key,
                          "unread": self.unread.get(peer.address, 0)})
        messages = [dict(item, state=self.state_of(item))
                    for item in self.threads.get(self.convo, [])[-80:]]
        base.update({
            "me": {"nick": session.nick, "address": self.identity.address,
                   "psm": session.psm, "status": session.status.value},
            "peers": peers,
            "convo": self.convo,
            "unread_group": self.unread.get(GROUP, 0),
            "messages": messages,
            "airtime": self.airtime(),
        })
        for panel in self.panels:
            try:
                base[panel.tag] = panel.snapshot()
            except Exception:
                base[panel.tag] = {}
        return base

    def absorb(self, event) -> None:
        from .session import AppEvent
        if isinstance(event, AppEvent):
            for panel in self.panels:
                if panel.tag == event.app:
                    try:
                        panel.heard(event.src, event.payload)
                    except Exception as exc:
                        self.note(f"{panel.title} stumbled: {exc}", "warn")
            return
        if isinstance(event, MessageEvent):
            thread = self.threads.setdefault(event.convo, [])
            thread.append({"who": event.who, "text": event.text,
                           "mine": not event.incoming, "seq": event.seq,
                           "when": int(event.when)})
            del thread[:-200]
            if event.incoming and event.convo != self.convo:
                self.unread[event.convo] = self.unread.get(event.convo, 0) + 1
        elif isinstance(event, SystemEvent):
            self.note(event.text, "warn" if event.level == "warn" else "muted")
        elif isinstance(event, PresenceEvent):
            self.note(event.text)

    def state_of(self, item) -> str:
        if item["mine"] and self.client is not None:
            out = self.client.session.outgoing(item["seq"])
            return out.state.value if out is not None else ""
        return ""

    def airtime(self) -> str:
        for bearer in self.link.interfaces if self.link else []:
            budget = getattr(bearer, "budget", None)
            if budget is not None:
                return f"{budget.remaining_ms(time.time())/1000:.0f} s"
        return ""

    def run(self) -> None:
        # One at a time. Two copies would fight over the serial port and the
        # loser would look broken rather than second, so the second one just
        # opens a window onto the first and gets out of the way.
        if not service.only_one(self.port):
            print(f"loraline is already running; opening its window.", flush=True)
            service.raise_window(self.port)
            return
        self.start_server()
        if self.settings.ready:
            threading.Thread(target=self._bring_up, daemon=True).start()
        else:
            self.look_for_radio()
        try:
            webbrowser.open(f"http://127.0.0.1:{self.port}")
        except Exception:
            pass
        print(f"loraline is at http://127.0.0.1:{self.port}")
        while self.running:
            now = time.time()
            if self.client is not None:
                for event in self.client.pump():
                    self.absorb(event)
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
            time.sleep(0.2)

    def drain(self) -> list:
        out = []
        while True:
            try:
                out.append(self.inbox.get_nowait())
            except queue.Empty:
                return out


def main(argv=None) -> int:
    App().run()
    return 0


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
      display:flex;flex-direction:column}
header{display:flex;align-items:baseline;justify-content:space-between;gap:1rem;
       padding:1rem 0 .7rem;border-bottom:1px solid var(--rule)}
h1{font-size:1.1rem;margin:0;font-weight:600}
h1 span{color:var(--mark);font-family:var(--mono)}
.meta{font-family:var(--mono);font-size:.72rem;color:var(--faint);text-align:right}
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
.line{margin:0 0 .45rem}
.line b{font-weight:600}
.line .addr{font-family:var(--mono);font-size:.7rem;color:var(--faint)}
.line.mine b{color:var(--mark)}
.tick{font-family:var(--mono);font-size:.7rem;color:var(--faint);margin-left:.3rem}
.tick.d{color:var(--good)}.tick.f{color:var(--mark)}
form.say{display:flex;gap:.5rem;padding-top:.5rem;border-top:1px solid var(--rule)}
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
@media(max-width:40rem){.cols{flex-direction:column}.side{width:auto}}
</style></head><body>
<div class="wrap">
<header>
  <h1><span>&#x2571;&#x2571;&#x2572;</span> loraline</h1>
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
      ${answered.length === 1 ? 'Found it.' :
        answered.length > 1 ? 'More than one answered. Pick whichever you meant.' :
        'Nothing answered yet.'}
      <button class="small" type="button" onclick="send({do:'look'})">look again</button>
    </p>

    ${state.busy ? `<p class="busy">${esc(state.busy)}\u2026</p>` : ''}
    ${state.trouble ? `<p class="trouble">${esc(state.trouble)}</p>` : ''}

    <button class="go" type="button" onclick="begin()">Start</button>
  </div>`;
}

function begin(){
  send({do:'begin',
        nick: document.getElementById('nick').value,
        passphrase: document.getElementById('pass').value,
        band: document.getElementById('band').value,
        port: document.getElementById('port').value});
}

const TICK = {q:'\u00b7', s:'\u00b7', p:'\u2713', d:'\u2713\u2713', f:'\u2717'};

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
      <span>start when the computer does<br>
      <span class="hint">${esc(s.how || '')}</span></span></label>`;
}

function talking(){
  const me = state.me || {}, peers = state.peers || [];
  const here = peers.filter(p => p.status !== 'x');
  return `
  <div class="cols">
    <div class="side">
      <h3>this window</h3>
      <p class="staying">${staying()}</p>
      <h3>conversations</h3>
      <button class="who" ${state.convo==='*'?'aria-current="page"':''}
        onclick="send({do:'convo',convo:'*'})">
        ${state.unread_group ? `<span class="pip">${state.unread_group}</span>` : ''}
        everybody</button>
      <h3>in range</h3>
      ${here.length ? here.map(p => `
        <button class="who" ${state.convo===p.address?'aria-current="page"':''}
          onclick="send({do:'convo',convo:'${p.address}'})">
          ${p.unread ? `<span class="pip">${p.unread}</span>` : ''}
          <span class="dot ${p.status}"></span>${esc(p.name)}
          ${p.psm ? `<span class="psm">${esc(p.psm)}</span>` : ''}
        </button>`).join('') : '<p class="hint">Nobody yet. They appear on their own.</p>'}
    </div>
    <div class="talk">
      <div class="lines" id="lines">
        ${(state.messages||[]).map(m => `
          <p class="line ${m.mine?'mine':''}"><b>${esc(m.who)}</b>
            ${m.text ? esc(m.text) : ''}
            ${m.mine ? `<span class="tick ${m.state}">${TICK[m.state]||''}</span>` : ''}
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

function say(e){
  e.preventDefault();
  const box = document.getElementById('text');
  if(box.value.trim()) send({do:'say', text: box.value.trim()});
  box.value = '';
}

function render(){
  const meta = document.getElementById('meta');
  if(state.ready && state.me){
    meta.innerHTML = `${esc(state.me.nick)} &middot; ${state.me.address}` +
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
    body.innerHTML = state.ready ? talking() : setup();
    body.dataset.view = want;
    Object.entries(values).forEach(([id, v]) => {
      const el = document.getElementById(id);
      if(el && v && !el.value) el.value = v;
    });
    if(keep){ const el = document.getElementById(keep); if(el) el.focus(); }
    const lines = document.getElementById('lines');
    if(lines) lines.scrollTop = lines.scrollHeight;
  }
}

new EventSource('/events').onmessage = m => { state = JSON.parse(m.data); render(); };
</script></body></html>
"""
