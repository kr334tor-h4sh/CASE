"""
A plain, persistent, interactive browser window for direct human use - NOT
part of CASE's tool-calling loop (that's browse_url in case_browser.py,
which fetches one page, extracts text, and reports back to the model).

This is simpler on purpose: open a real window (same pywebview/WebView2
engine, no separate browser binary), default it to Google, and just leave
it running so Sati can type searches and click through results exactly
like a normal browser tab. No text extraction, no timeout, no reporting
back - it's for him to look at directly, not for CASE to read.

Standalone script, launched detached (never imported) - same reasoning as
case_browser_worker.py: GUI toolkits don't mix safely in one process, and
this way the window stays open independently of anything else CASE is doing.

Usage: pythonw.exe case_open_browser.py [optional_start_url]
"""

import sys

import webview

DEFAULT_URL = "https://www.google.com"


def main():
    start_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    webview.create_window(
        "CASE Browser",
        start_url,
        width=1200,
        height=850,
    )
    webview.start()  # blocks for this process's lifetime - window stays open/interactive


if __name__ == "__main__":
    main()
