"""
CASE's real browser window - a standalone worker process (launched detached
by case_browser.py, never imported directly). Uses pywebview, which drives
the OS's already-installed web engine (WebView2/Edge on Windows) instead of
bundling a separate browser binary - the lightest real option available.

Runs as its own process on purpose: two independent pywebview windows (this
+ case_gui_web.py's own) shouldn't share one event loop, and this design also
means the window can stay open and interactive for Sati indefinitely
without blocking CASE's tool-call loop waiting for it to close.

Usage (invoked by case_browser.py, not run directly):
    pythonw.exe case_browser_worker.py <url> <output_text_file>
"""

import sys
import time

import webview

EXTRACT_DELAY_SECONDS = 1.5  # let dynamic/JS content settle after load fires
MAX_TEXT_CHARS = 20_000


def main():
    if len(sys.argv) < 3:
        return
    url = sys.argv[1]
    output_file = sys.argv[2]

    window = webview.create_window(f"CASE Browser — {url}", url, width=1150, height=820)

    def on_loaded():
        time.sleep(EXTRACT_DELAY_SECONDS)
        try:
            text = window.evaluate_js("document.body.innerText") or ""
        except Exception as e:
            text = f"(could not extract page text: {e})"
        text = text[:MAX_TEXT_CHARS]
        try:
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass

    window.events.loaded += on_loaded
    webview.start()  # blocks HERE for this process's lifetime - window stays open/interactive


if __name__ == "__main__":
    main()
