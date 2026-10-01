"""
Backend for the browser panel INSIDE the CASE window (case_gui_web.py).

pywebview cannot host a second web view inside its own window, and most big
sites refuse to load in an iframe, so the panel is a live VIEW of a real
Edge/Chrome tab instead: this module takes screenshots of the tab over the
local debugging port (CDP) and forwards the user's clicks, scrolling and
typing back to it. It works on any site. When the GUI runs, case_browser is
switched to headless mode (case_browser.EMBEDDED), so the only place the
browser is ever visible is this panel - no separate window.

The model's own browse_url / open_browser_window go through the same browser,
so whatever it loads shows up here and the panel jumps to that tab (see
case_browser.AGENT_STATE, polled by panel_frame).

All functions are plain, JSON-friendly and thread-safe (pywebview calls the
Api from worker threads), and never raise - they return {"error": ...}.
"""

import base64
import hashlib
import json
import threading
import urllib.parse

import case_browser as cb

_lock = threading.RLock()
_conn = {"tab_id": None, "ws": None, "mid": 0}
_state = {"current": None, "seen_agent_seq": 0, "last_hash": None}

# JPEG quality trades sharpness for speed - the panel polls a few times a second.
JPEG_QUALITY = 60

_SPECIAL_KEYS = {
    "Enter": (13, "\r"), "Backspace": (8, None), "Tab": (9, "\t"), "Escape": (27, None),
    "Delete": (46, None), "ArrowLeft": (37, None), "ArrowUp": (38, None),
    "ArrowRight": (39, None), "ArrowDown": (40, None), "Home": (36, None),
    "End": (35, None), "PageUp": (33, None), "PageDown": (34, None),
}
_CTRL_COMMANDS = {"a": "selectAll", "c": "copy", "x": "cut", "z": "undo", "y": "redo"}


def available() -> bool:
    """Whether a debuggable browser exists (or can be started) at all."""
    return cb._browser_exe() is not None


def _drop_conn():
    ws = _conn["ws"]
    _conn["ws"] = None
    _conn["tab_id"] = None
    if ws is not None:
        try:
            ws.close()
        except Exception:
            pass


def _current_tab_id() -> str:
    """The tab the panel shows: whatever was last chosen, if it still exists,
    else CASE's working tab."""
    tabs = cb._page_tabs()
    ids = [t["id"] for t in tabs]
    if _state["current"] in ids:
        return _state["current"]
    tab = cb._work_tab()
    _state["current"] = tab["id"]
    return tab["id"]


def _cdp(method: str, params: dict = None, wait: float = 10) -> dict:
    """One CDP command on the panel's tab, over a persistent connection that
    is re-opened if the tab changed or the socket died."""
    import time
    from websockets.sync.client import connect

    with _lock:
        last_err = None
        for _ in range(2):
            try:
                tab_id = _current_tab_id()
                if _conn["ws"] is None or _conn["tab_id"] != tab_id:
                    _drop_conn()
                    target = next(t for t in cb._page_tabs() if t["id"] == tab_id)
                    _conn["ws"] = connect(target["webSocketDebuggerUrl"], max_size=None, open_timeout=10)
                    _conn["tab_id"] = tab_id
                    _state["last_hash"] = None
                _conn["mid"] += 1
                mid = _conn["mid"]
                ws = _conn["ws"]
                ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                end = time.time() + wait
                while True:
                    msg = json.loads(ws.recv(timeout=max(0.1, end - time.time())))
                    if msg.get("id") == mid:
                        if "error" in msg:
                            raise RuntimeError(msg["error"].get("message", "CDP error"))
                        return msg.get("result", {})
            except Exception as e:
                last_err = e
                _drop_conn()
        raise RuntimeError(str(last_err))


def _safe(fn):
    def wrapper(*a, **k):
        try:
            if not cb._ensure_browser():
                return {"error": "No Edge/Chrome browser found."}
            return fn(*a, **k)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
    wrapper.__name__ = fn.__name__
    return wrapper


def _tab_list() -> list:
    cur = _current_tab_id()
    return [
        {"id": t["id"], "title": (t.get("title") or t.get("url") or "New tab")[:40], "url": t.get("url", ""),
         "active": t["id"] == cur}
        for t in cb._page_tabs()
    ]


@_safe
def frame(last_hash: str = "") -> dict:
    """The panel's polling call: current screenshot (omitted when unchanged
    since last_hash), the tab strip, and the current URL. Also follows the
    model: if it loaded something since the last poll, jump to that tab."""
    switched = False
    seq = cb.AGENT_STATE["seq"]
    if seq != _state["seen_agent_seq"]:
        _state["seen_agent_seq"] = seq
        if cb.AGENT_STATE["tab_id"]:
            _state["current"] = cb.AGENT_STATE["tab_id"]
            switched = True
    tabs = _tab_list()
    cur = next((t for t in tabs if t["active"]), None) or {"url": "", "title": ""}
    out = {"tabs": tabs, "url": cur["url"], "title": cur["title"], "agent_seq": seq, "switched": switched}
    shot = _cdp("Page.captureScreenshot", {"format": "jpeg", "quality": JPEG_QUALITY})
    data = shot.get("data", "")
    digest = hashlib.md5(data.encode("ascii", "ignore")).hexdigest()
    out["hash"] = digest
    if digest != last_hash:
        out["img"] = "data:image/jpeg;base64," + data
    return out


def poll() -> dict:
    """Cheap call (no screenshot) the GUI makes even while the panel is
    hidden, so it can pop the panel open when the model uses the browser."""
    return {"agent_seq": cb.AGENT_STATE["seq"]}


@_safe
def set_viewport(width: int, height: int) -> dict:
    w, h = max(200, int(width)), max(150, int(height))
    _cdp("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False})
    return {"ok": True}


def _mouse(kind: str, x: float, y: float, button: str = "left", clicks: int = 1, **extra):
    _cdp("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": button,
                                      "clickCount": clicks, **extra})


@_safe
def click(x: float, y: float, button: str = "left", clicks: int = 1) -> dict:
    _mouse("mouseMoved", x, y, "none", 0)
    _mouse("mousePressed", x, y, button, clicks, buttons=1 if button == "left" else 2)
    _mouse("mouseReleased", x, y, button, clicks)
    return {"ok": True}


@_safe
def scroll(x: float, y: float, dx: float, dy: float) -> dict:
    _cdp("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y, "deltaX": dx, "deltaY": dy})
    return {"ok": True}


@_safe
def key(key_name: str, ctrl: bool = False, shift: bool = False, alt: bool = False) -> dict:
    mods = (1 if alt else 0) | (2 if ctrl else 0) | (8 if shift else 0)
    if ctrl and key_name.lower() in _CTRL_COMMANDS:
        _cdp("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": key_name, "modifiers": mods,
                                         "commands": [_CTRL_COMMANDS[key_name.lower()]]})
        _cdp("Input.dispatchKeyEvent", {"type": "keyUp", "key": key_name, "modifiers": mods})
        return {"ok": True}
    if key_name in _SPECIAL_KEYS:
        vk, text = _SPECIAL_KEYS[key_name]
        down = {"type": "keyDown" if text else "rawKeyDown", "key": key_name, "windowsVirtualKeyCode": vk, "modifiers": mods}
        if text:
            down["text"] = text
        _cdp("Input.dispatchKeyEvent", down)
        _cdp("Input.dispatchKeyEvent", {"type": "keyUp", "key": key_name, "windowsVirtualKeyCode": vk, "modifiers": mods})
        return {"ok": True}
    if len(key_name) == 1 and not ctrl:
        _cdp("Input.dispatchKeyEvent", {"type": "keyDown", "key": key_name, "text": key_name,
                                         "unmodifiedText": key_name, "modifiers": mods})
        _cdp("Input.dispatchKeyEvent", {"type": "keyUp", "key": key_name, "modifiers": mods})
    return {"ok": True}


@_safe
def insert_text(text: str) -> dict:
    """For paste - the browser's own clipboard is separate from Windows'."""
    _cdp("Input.insertText", {"text": text or ""})
    return {"ok": True}


def _to_url(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return "about:blank"
    if t.startswith(("http://", "https://", "about:", "file:", "data:")):
        return t
    if " " not in t and "." in t:
        return "https://" + t
    return "https://www.bing.com/search?" + urllib.parse.urlencode({"q": t})


@_safe
def navigate(text: str) -> dict:
    _cdp("Page.navigate", {"url": _to_url(text)})
    return {"ok": True}


@_safe
def history(step: int) -> dict:
    """step -1 = back, +1 = forward."""
    nav = _cdp("Page.getNavigationHistory")
    idx = nav["currentIndex"] + int(step)
    entries = nav["entries"]
    if 0 <= idx < len(entries):
        _cdp("Page.navigateToHistoryEntry", {"entryId": entries[idx]["id"]})
    return {"ok": True}


@_safe
def reload() -> dict:
    _cdp("Page.reload")
    return {"ok": True}


@_safe
def new_tab(url: str = "") -> dict:
    target = _to_url(url) if url else "about:blank"
    tab = cb._cdp_get("/json/new?" + urllib.parse.quote(target, safe=":/?&=%#"), method="PUT")
    _state["current"] = tab["id"]
    return {"ok": True, "id": tab["id"]}


@_safe
def switch_tab(tab_id: str) -> dict:
    _state["current"] = tab_id
    try:
        cb._cdp_get(f"/json/activate/{tab_id}")
    except Exception:
        pass
    return {"ok": True}


@_safe
def close_tab(tab_id: str) -> dict:
    if _state["current"] == tab_id:
        _state["current"] = None
        with _lock:
            _drop_conn()
    cb._cdp_get(f"/json/close/{tab_id}")
    if not cb._page_tabs():  # never leave the browser with zero tabs
        cb._cdp_get("/json/new?about:blank", method="PUT")
    return {"ok": True}
