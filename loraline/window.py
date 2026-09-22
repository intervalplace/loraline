"""A window of loraline's own, rather than a tab pointed at an address.

The server and the page are unchanged: this opens the same HTML in the
operating system's own web view, so there is no address bar, no tab, and
nothing saying 127.0.0.1 to somebody who only wanted to talk to a friend.

Every path stays open. A machine with no web view, a Pi with no screen, and
anybody who would rather use their browser all carry on working exactly as
before, because the thing being shown is a web page either way.
"""
from __future__ import annotations

import os
import sys
import threading


# Set once the web view is actually up, so the app can say which way it went
# rather than leaving somebody to guess from the absence of a window.
showing = False


def wanted() -> bool:
    """Whether to try for a window of our own.

    LORALINE_WINDOW=0 forces the browser, =1 insists on the window and says so
    if it cannot. Neither set means try the window and fall back quietly.
    """
    choice = os.environ.get("LORALINE_WINDOW", "").strip().lower()
    if choice in ("0", "no", "off", "browser"):
        return False
    if os.environ.get("LORALINE_NO_BROWSER"):
        return False          # a build server has neither
    return True


def available() -> bool:
    try:
        import webview            # noqa: F401
    except Exception:
        return False
    return True


def why_not() -> str:
    """What to tell somebody who asked for a window and cannot have one."""
    if not wanted():
        return "asked for the browser"
    if not available():
        try:
            import webview          # noqa: F401
        except Exception as exc:
            return f"no web view here ({type(exc).__name__}: {exc})"
        return "no web view here"
    return ""


# What the largest thing in here needs.
#
# The coast is 720 by 476, and above and below it sit the switcher, the
# channel warning, the composer and the log: about 744 by 670 in all. The
# window used to open at 1080 by 720, which left twenty pixels of headroom
# before the title bar took them, so the shore arrived already cramped and
# everybody went straight to full screen.
WANTS = (1140, 880)

# And never smaller than this.
#
# 760 keeps the chat side by side rather than stacked, and leaves the coast's
# 720 pixel framebuffer whole. 560 leaves room for the conversation, the
# composer and the log at once. Below either, things are reachable but the
# window is doing the person a disservice, so it will not go there.
FLOOR = (760, 560)


def fits() -> tuple:
    """The size to open at: what the views need, or the screen if it is
    smaller. A window taller than the display is worse than a small one."""
    wide, tall = WANTS
    # Asking about the screens makes pywebview go looking for a backend, and
    # on a machine without one that is a page of tracebacks on the way to a
    # window we were not going to get anyway. The answer is optional; the
    # noise is not worth it.
    #
    # It logs rather than prints, and its handler kept hold of stderr when it
    # was made, so redirecting stderr afterwards catches nothing.
    import logging
    chatter = logging.getLogger("pywebview")
    was = chatter.level
    try:
        import webview
        chatter.setLevel(logging.CRITICAL)
        screens = getattr(webview, "screens", None) or []
        chatter.setLevel(was)
        if screens:
            room = screens[0]
            wide = min(wide, int(room.width * 0.92))
            tall = min(tall, int(room.height * 0.90))
    except Exception:
        pass
    finally:
        chatter.setLevel(was)
    return max(FLOOR[0], wide), max(FLOOR[1], tall)


def show(url: str, title: str = "loraline", serve=None) -> bool:
    """Open the window and block until it is closed.

    Returns False if there is no web view here, so the caller can fall back to
    a browser. `serve` is the thing that must keep running while the window is
    up; it is started on a thread of its own, because the web view has to own
    the main thread on macOS.
    """
    if not (wanted() and available()):
        return False
    try:
        import webview
    except Exception:
        return False

    wide, tall = fits()

    if serve is not None:
        threading.Thread(target=serve, daemon=True).start()

    try:
        webview.create_window(title, url, width=wide, height=tall,
                              min_size=FLOOR, text_select=True)
        # http_server=False: the page is already being served by loraline
        # itself, and letting the web view start a second one would be a
        # second answer to the same question.
        global showing
        showing = True
        webview.start(debug=bool(os.environ.get("LORALINE_WINDOW_DEBUG")))
        return True
    except Exception as exc:
        # start() raises when there is no backend, and it raises after the
        # flag was set, so the app would have reported a window it never got.
        showing = False
        print(f"Could not open a window ({exc}); using the browser instead.",
              file=sys.stderr)
        return False
