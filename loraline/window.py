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

    if serve is not None:
        threading.Thread(target=serve, daemon=True).start()

    try:
        webview.create_window(title, url, width=1080, height=720,
                              min_size=(360, 480), text_select=True)
        # http_server=False: the page is already being served by loraline
        # itself, and letting the web view start a second one would be a
        # second answer to the same question.
        global showing
        showing = True
        webview.start(debug=bool(os.environ.get("LORALINE_WINDOW_DEBUG")))
        return True
    except Exception as exc:
        print(f"Could not open a window ({exc}); using the browser instead.",
              file=sys.stderr)
        return False
