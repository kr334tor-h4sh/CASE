"""
web_search tool for CASE. Tries Brave Search API first (reliable, real
sanctioned API - no scraping-detection risk), falls back to DuckDuckGo's
HTML scrape if no Brave key is configured or Brave itself errors.

Why the fallback chain, not a straight replacement: DuckDuckGo's free
scraping approach proved genuinely unreliable in practice (hit a real,
long-lasting rate-limit block during actual use, not just testing) - Brave
fixes that, but Sati is still looking for something with zero rate-limit
risk at all, so DuckDuckGo stays as a free backup rather than being ripped
out, in case Brave's own free-tier quota (2,000/month) ever runs dry mid-month.

Deliberately NOT Perplexity - that's a different, paid, synthesis-style
product, a separate decision Sati explicitly didn't ask for.

API key setup: put a real key in case_config.json's "brave_api_key" field
(get one free at https://brave.com/search/api/ - Sati has to sign up
himself, account creation isn't something this tool/Claude can do). Empty
key = Brave is skipped, DuckDuckGo is used directly, no error.

Read-only, no write-confirmation needed - registered into case_tools.py's
shared tool list (case_cli.py, case_gui_web.py, AND Bionic-native alike).
"""

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "case_config.json"
REQUEST_TIMEOUT = 15
MAX_RESULTS = 6

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
DDG_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

_TITLE_RE = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(raw: str) -> str:
    """Strip HTML tags and unescape entities, collapse whitespace."""
    text = _TAG_RE.sub("", raw)
    text = html.unescape(text)
    return " ".join(text.split())


def _get_brave_key() -> str:
    if not CONFIG_FILE.exists():
        return ""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError):
        return ""
    return (config.get("brave_api_key") or "").strip()


def _brave_search(query: str, api_key: str) -> str | None:
    """Try Brave Search API. Returns formatted results, or None if it
    should fall back to DuckDuckGo (no key, bad key, quota exhausted, or
    any other failure) - errors here are silent-fallback, not user-facing,
    since DuckDuckGo is the safety net."""
    url = f"{BRAVE_URL}?{urllib.parse.urlencode({'q': query, 'count': MAX_RESULTS})}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None  # any failure here -> silently fall back to DuckDuckGo

    results = (data.get("web") or {}).get("results") or []
    if not results:
        return f"Web search results for '{query}': no results found (Brave Search)."

    lines = [f"Web search results for '{query}' (via Brave Search):\n"]
    for i, r in enumerate(results[:MAX_RESULTS]):
        title = _clean(r.get("title", ""))
        link = r.get("url", "")
        snippet = _clean(r.get("description", ""))
        lines.append(f"{i + 1}. {title}\n   {link}\n   {snippet}\n")
    return "\n".join(lines)


def _real_url(ddg_redirect_href: str) -> str:
    """DuckDuckGo's HTML results link through their own redirect
    (//duckduckgo.com/l/?uddg=<url-encoded-real-url>&rut=...) - pull the
    actual destination URL back out."""
    if ddg_redirect_href.startswith("//"):
        ddg_redirect_href = "https:" + ddg_redirect_href
    parsed = urllib.parse.urlparse(ddg_redirect_href)
    qs = urllib.parse.parse_qs(parsed.query)
    if "uddg" in qs:
        return qs["uddg"][0]
    return ddg_redirect_href


def _duckduckgo_search(query: str) -> str:
    url = f"{DDG_URL}?{urllib.parse.urlencode({'q': query})}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw_html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        return f"ERROR: web search failed - could not reach DuckDuckGo ({e}). Check internet connectivity."
    except Exception as e:
        return f"ERROR: web search failed - {type(e).__name__}: {e}"

    titles_and_links = _TITLE_RE.findall(raw_html)
    snippets = _SNIPPET_RE.findall(raw_html)

    if not titles_and_links:
        lowered = raw_html.lower()
        if "anomaly" in lowered or "unusual traffic" in lowered or "captcha" in lowered:
            return (
                f"ERROR: DuckDuckGo rate-limited or blocked this search (too many "
                f"requests too quickly). This is temporary - wait a bit and try again, "
                f"or ask the user to add a Brave Search API key for a reliable alternative. "
                f"Do not report this as 'no results exist for {query}'."
            )
        return f"No web results found for '{query}' (or DuckDuckGo's page structure changed - flag this if it keeps happening)."

    lines = [f"Web search results for '{query}' (via DuckDuckGo):\n"]
    for i, (href, title_raw) in enumerate(titles_and_links[:MAX_RESULTS]):
        title = _clean(title_raw)
        real_link = _real_url(href)
        snippet = _clean(snippets[i]) if i < len(snippets) else ""
        lines.append(f"{i + 1}. {title}\n   {real_link}\n   {snippet}\n")

    return "\n".join(lines)


def web_search(query: str) -> str:
    """Search the web for a query and return the top results (title, URL,
    snippet). Uses Brave Search (reliable) if configured, otherwise falls
    back to a free DuckDuckGo scrape. Best for current events, recent news,
    or anything outside the local project files/memory - use
    list_directory/read_file/memory_reflect first for anything about
    Sati's own projects, this is for everything else."""
    if not query or not query.strip():
        return "ERROR: empty search query."

    brave_key = _get_brave_key()
    if brave_key:
        result = _brave_search(query, brave_key)
        if result is not None:
            return result
        # Brave failed for some reason - fall through to DuckDuckGo silently

    return _duckduckgo_search(query)
