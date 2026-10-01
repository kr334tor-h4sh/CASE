"""
browse_url tool for CASE - opens a REAL, visible browser window (via
pywebview -> the OS's built-in WebView2/Edge engine, no separate browser
binary bundled) and returns the loaded page's text content.

Launches case_browser_worker.py as a detached background process so this
call doesn't block waiting for the whole window's lifetime - it waits only
long enough for the page to load and its text to be extracted, then
returns that text while the window itself stays open independently for
Sati to actually look at / keep browsing in.

Read-only from CASE's own tool-loop perspective (opening a window isn't a
filesystem modification), so this lives in the same no-confirmation tier
as web_search - available to case_cli.py, case_gui_web.py, and Bionic-native alike.
"""

import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

WORKER_SCRIPT = Path(__file__).parent / "case_browser_worker.py"
OPEN_BROWSER_SCRIPT = Path(__file__).parent / "case_open_browser.py"
# Same directory as whatever interpreter is running CASE right now - pythonw.exe
# always ships alongside python.exe in a standard install, so this resolves
# correctly regardless of which one launched this process or where Python
# is actually installed (a hardcoded absolute path broke on anything but
# the one machine/Python version it was written for).
PYTHONW_EXE = Path(sys.executable).parent / "pythonw.exe"
WAIT_TIMEOUT_SECONDS = 30
POLL_INTERVAL = 0.5

# --- Tabbed browser (2026-10-01) ------------------------------------------
# One dedicated Edge/Chrome window, driven over its local debugging port
# (CDP), instead of a NEW pywebview window per call. browse_url reuses a
# single working tab; open_browser_window adds a tab to the SAME window.
# pywebview can't do tabs (one page per window) and iframes are blocked by
# most big sites, so a real browser is the only way to get real tabs.
# Falls back to the old per-call pywebview window if no Edge/Chrome is found
# or the debugging connection fails. The profile folder is separate from
# the user's own browser profile (no cookies/logins shared, none of their tabs touched).
import os

CDP_PORT = int(os.environ.get("CASE_CDP_PORT", "9333"))  # env override is for side-by-side testing
CDP_BASE = f"http://127.0.0.1:{CDP_PORT}"
PROFILE_DIR = Path(os.environ.get("CASE_BROWSER_PROFILE") or (Path(__file__).parent / "browser_profile"))
# Set True by case_gui_web.py: the browser then starts HEADLESS (no window of
# its own) and is shown only inside the CASE window's browser panel
# (case_browser_panel.py). CLI / MCP use leaves it False = a normal window.
EMBEDDED = False
# Bumped whenever the MODEL loads a page or opens a tab, so the panel can
# pop open and jump to that tab. tab_id = the tab it just used.
AGENT_STATE = {"seq": 0, "tab_id": None}
WORK_TAB_FILE = PROFILE_DIR / "case_work_tab.txt"
EXTRACT_DELAY_SECONDS = 1.5
MAX_TEXT_CHARS = 8_000  # page text handed to the model; 20K chars x several pages overflowed a 16K context
_BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _cdp_get(path: str, method: str = "GET", timeout: float = 3):
    req = urllib.request.Request(CDP_BASE + path, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(body)
    except ValueError:
        return body


def _browser_exe():
    for c in _BROWSER_CANDIDATES:
        if Path(c).exists():
            return c
    return None


MODE_FILE = PROFILE_DIR / "launch_mode.txt"


def _launch_mode() -> str:
    """How the running CASE browser was started: 'headless' or 'window'.
    Unknown (file missing, e.g. started by an older version) counts as 'window'."""
    try:
        return MODE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "window"


def _kill_case_browser() -> None:
    """Close CASE's OWN browser instance - only processes whose command line
    names CASE's private profile folder, so the user's own Edge/Chrome windows
    (a different profile) are never touched."""
    if sys.platform != "win32":
        return
    marker = str(PROFILE_DIR).replace("'", "''")
    ps = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -in 'msedge.exe','chrome.exe' -and "
        f"$_.CommandLine -like '*{marker}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return
    for _ in range(20):  # wait for the debugging port to go down
        time.sleep(0.25)
        try:
            _cdp_get("/json/version", timeout=1)
        except (urllib.error.URLError, OSError, ValueError):
            return


def _ensure_browser() -> bool:
    """True once a debuggable browser is up (starts one if needed). In
    EMBEDDED mode (the CASE GUI) the browser must be headless: a visible
    instance left over from CLI use is closed and restarted hidden, so the
    only place the browser ever shows is the panel inside the CASE window."""
    try:
        _cdp_get("/json/version")
        if not EMBEDDED or _launch_mode() == "headless":
            return True
        _kill_case_browser()
    except (urllib.error.URLError, OSError, ValueError):
        pass
    exe = _browser_exe()
    if not exe:
        return False
    PROFILE_DIR.mkdir(exist_ok=True)
    popen_kwargs = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    args = [exe, f"--remote-debugging-port={CDP_PORT}", "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={PROFILE_DIR}", "--no-first-run", "--no-default-browser-check"]
    if EMBEDDED:
        args += ["--headless=new", "--window-size=1100,800", "about:blank"]
    else:
        args += ["--new-window", "about:blank"]
    try:
        MODE_FILE.write_text("headless" if EMBEDDED else "window", encoding="utf-8")
    except OSError:
        pass
    try:
        subprocess.Popen(args, close_fds=True, **popen_kwargs)
    except OSError:
        return False
    for _ in range(40):
        time.sleep(0.5)
        try:
            _cdp_get("/json/version")
            return True
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return False


def _page_tabs() -> list:
    # Skip the browser's own internal pages (extension pages, the sync
    # prompt a fresh profile opens) - they are not tabs the user opened.
    hidden = ("chrome-extension://", "edge://sync-confirmation", "devtools://", "chrome://sync")
    return [t for t in _cdp_get("/json/list")
            if t.get("type") == "page" and not t.get("url", "").startswith(hidden)]


def _work_tab() -> dict:
    """CASE's own working tab (the one browse_url reuses) - found by the id
    remembered from last time if it still exists, else the blank tab the
    browser opened on launch, else a fresh one. The user's own tabs are never
    navigated or read."""
    tabs = _page_tabs()
    try:
        wanted = WORK_TAB_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        wanted = ""
    tab = next((t for t in tabs if t["id"] == wanted), None)
    if tab is None:
        tab = next((t for t in tabs if t.get("url") == "about:blank"), None)
    if tab is None:
        tab = _cdp_get("/json/new?about:blank", method="PUT")
    try:
        WORK_TAB_FILE.write_text(tab["id"], encoding="utf-8")
    except OSError:
        pass
    return tab


def _tab_browse(url: str) -> str:
    """Load url in the working tab and return its text. Raises on any
    failure so browse_url can fall back to the old window."""
    from websockets.sync.client import connect

    tab = _work_tab()
    with connect(tab["webSocketDebuggerUrl"], max_size=None, open_timeout=10) as ws:
        counter = [0]

        def call(method, params=None, wait=15):
            counter[0] += 1
            mid = counter[0]
            ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            end = time.time() + wait
            while True:
                msg = json.loads(ws.recv(timeout=max(0.1, end - time.time())))
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise RuntimeError(msg["error"].get("message", "CDP error"))
                    return msg.get("result", {})

        call("Page.enable")
        call("Page.navigate", {"url": url})
        loaded = True
        end = time.time() + WAIT_TIMEOUT_SECONDS
        while True:
            try:
                msg = json.loads(ws.recv(timeout=max(0.1, end - time.time())))
            except TimeoutError:
                loaded = False
                break
            if msg.get("method") == "Page.loadEventFired":
                break
        time.sleep(EXTRACT_DELAY_SECONDS)
        res = call("Runtime.evaluate", {"expression": "document.body ? document.body.innerText : ''", "returnByValue": True})
        text = (res.get("result", {}).get("value") or "")[:MAX_TEXT_CHARS]
    try:
        _cdp_get(f"/json/activate/{tab['id']}")  # bring the tab forward so the user can see it
    except (urllib.error.URLError, OSError):
        pass
    AGENT_STATE["tab_id"] = tab["id"]
    AGENT_STATE["seq"] += 1
    note = "" if loaded else f" (the page had not finished loading after {WAIT_TIMEOUT_SECONDS}s, so this text may be partial)"
    return (
        f"Loaded {url} in CASE's browser window (working tab; it stays open "
        f"for the user to look at/use){note}. Extracted page text:\n\n{text}"
    )


def open_browser_window(url: str = "") -> str:
    """Open a plain, persistent, interactive browser window for Sati to use
    directly - defaults to Google if no URL given. Unlike browse_url, this
    does NOT extract text or report content back - it's for direct human
    use (typing searches, clicking results, browsing normally), not for
    CASE to read. Returns immediately once the window is launched, doesn't
    wait for anything."""
    try:
        if _ensure_browser():
            target = url.strip() if url and url.strip() else "https://www.google.com"
            tab = _cdp_get("/json/new?" + urllib.parse.quote(target, safe=":/?&=%#"), method="PUT")
            _cdp_get(f"/json/activate/{tab['id']}")
            AGENT_STATE["tab_id"] = tab["id"]
            AGENT_STATE["seq"] += 1
            return f"Opened a new tab at {target} in CASE's browser window for the user to use directly."
    except Exception:
        pass  # fall through to the old standalone window below

    args = [str(PYTHONW_EXE), str(OPEN_BROWSER_SCRIPT)]
    if url and url.strip():
        args.append(url.strip())

    popen_kwargs = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    try:
        subprocess.Popen(args, close_fds=True, **popen_kwargs)
    except OSError as e:
        return f"ERROR: could not open the browser window - {e}"

    target = url.strip() if url and url.strip() else "Google"
    return f"Opened a browser window at {target} for the user to use directly."


def browse_url(url: str) -> str:
    """Open a real, visible browser window showing this URL (stays open for
    Sati to look at/use directly) and return the page's extracted text.
    Use this when a question needs to actually SEE a real page (current
    content, something web_search's snippets don't cover in enough detail),
    not for a quick fact lookup - web_search is faster for that."""
    if not url or not url.strip():
        return "ERROR: empty URL."
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url

    try:
        if _ensure_browser():
            return _tab_browse(url)
    except Exception:
        pass  # any debugging-port/websocket problem -> fall through to the old window below

    if not PYTHONW_EXE.exists():
        return f"ERROR: pythonw.exe not found at {PYTHONW_EXE}."

    out_file = Path(tempfile.gettempdir()) / f"case_browse_{uuid.uuid4().hex}.txt"

    popen_kwargs = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )

    try:
        subprocess.Popen(
            [str(PYTHONW_EXE), str(WORKER_SCRIPT), url, str(out_file)],
            close_fds=True,
            **popen_kwargs,
        )
    except OSError as e:
        return f"ERROR: could not launch the browser window - {e}"

    waited = 0.0
    while waited < WAIT_TIMEOUT_SECONDS:
        if out_file.exists():
            time.sleep(0.3)  # brief grace period in case the write is still in-flight
            try:
                text = out_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            try:
                out_file.unlink()
            except OSError:
                pass
            return (
                f"Opened a live browser window showing {url} (it stays open "
                f"for the user to look at/use). Extracted page text:\n\n{text}"
            )
        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL

    return (
        f"Opened a browser window for {url}, but its text wasn't ready within "
        f"{WAIT_TIMEOUT_SECONDS}s (the page may still be loading, e.g. slow/heavy "
        f"site). The window itself should still be open on screen."
    )
