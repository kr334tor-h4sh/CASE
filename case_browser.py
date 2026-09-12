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

import subprocess
import sys
import tempfile
import time
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


def open_browser_window(url: str = "") -> str:
    """Open a plain, persistent, interactive browser window for Sati to use
    directly - defaults to Google if no URL given. Unlike browse_url, this
    does NOT extract text or report content back - it's for direct human
    use (typing searches, clicking results, browsing normally), not for
    CASE to read. Returns immediately once the window is launched, doesn't
    wait for anything."""
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
