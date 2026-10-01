"""
fetch_url + youtube_transcript tools for CASE. Read-only, no write
confirmation needed - registered into case_tools.py's shared tool list.

fetch_url: a plain HTTP GET that returns the page as readable text, with NO
window opened. The lighter sibling of browse_url - use it for static pages,
RSS feeds, JSON APIs (SEC EDGAR etc.) and long articles. It cannot run
JavaScript, so a client-rendered site comes back as an empty shell; that is
when browse_url is the right tool instead.

youtube_transcript: the spoken text of ONE YouTube video, via the optional
youtube-transcript-api package (pip install youtube-transcript-api). Missing
package or a video without captions gives a plain ERROR string, never a crash.
"""

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request

REQUEST_TIMEOUT = 20
MAX_CHARS = 20000
MAX_BYTES = 3_000_000
# SEC EDGAR rejects requests without a descriptive User-Agent; a normal
# browser UA works everywhere else too.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 CASE-agent"

_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript|svg)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</?(p|div|br|li|tr|h[1-6]|section|article|header|footer)\b[^>]*>", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKS_RE = re.compile(r"\n\s*\n+")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)


def _html_to_text(raw: str) -> str:
    title_match = _TITLE_RE.search(raw)
    title = html.unescape(title_match.group(1)).strip() if title_match else ""
    body = _SCRIPT_STYLE_RE.sub(" ", raw)
    body = _BLOCK_RE.sub("\n", body)
    body = html.unescape(_TAG_RE.sub(" ", body))
    body = _WS_RE.sub(" ", body)
    body = "\n".join(line.strip() for line in body.split("\n"))
    body = _BLANKS_RE.sub("\n\n", body).strip()
    return (f"TITLE: {title}\n\n" if title else "") + body


def fetch_url(url: str, offset: int = 0) -> str:
    """Fetch a URL with a plain HTTP GET and return its readable text (HTML
    stripped; JSON/RSS/plain text returned as is). No browser window opens.
    Returns at most 20,000 characters per call - if the result says it was
    truncated, call again with offset set to the next character position.
    Use for static pages, articles, RSS feeds and JSON APIs. If the text
    that comes back is empty or only navigation boilerplate, the site
    renders in JavaScript - use browse_url for it instead."""
    if not url or not url.strip():
        return "ERROR: empty URL."
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0

    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            ctype = resp.headers.get("Content-Type", "")
            charset = resp.headers.get_content_charset() or "utf-8"
            raw_bytes = resp.read(MAX_BYTES + 1)
            final_url = resp.geturl()
    except urllib.error.HTTPError as e:
        return f"ERROR: {url} returned HTTP {e.code} {e.reason}."
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return f"ERROR: could not fetch {url} - {e}."

    if any(t in ctype.lower() for t in ("image/", "video/", "audio/", "application/pdf", "application/zip", "octet-stream")):
        return f"ERROR: {url} is {ctype or 'a binary file'}, not readable text."

    cut_bytes = len(raw_bytes) > MAX_BYTES
    text = raw_bytes[:MAX_BYTES].decode(charset, errors="replace")
    if "html" in ctype.lower() or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        text = _html_to_text(text)

    total = len(text)
    chunk = text[offset:offset + MAX_CHARS]
    header = f"FETCHED {final_url} ({ctype or 'unknown type'})\n\n"
    if not chunk:
        return header + f"(no text at offset {offset}; page text is {total} characters long)"
    end = offset + len(chunk)
    footer = ""
    if end < total:
        footer = f"\n\n[TRUNCATED - showing characters {offset}-{end} of {total}. Call fetch_url again with offset={end} for the next part.]"
    elif cut_bytes:
        footer = f"\n\n[Page was larger than {MAX_BYTES // 1_000_000} MB; the rest was not downloaded.]"
    return header + chunk + footer


_YT_ID_RE = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/))([A-Za-z0-9_-]{11})")


def youtube_transcript(url: str, offset: int = 0) -> str:
    """Get the spoken transcript (captions) of ONE specific YouTube video,
    given its link or 11-character id. Also fetches the video's title via
    YouTube's oEmbed endpoint. For a channel or playlist episode LIST use
    the context-fetch skill instead. Returns at most 20,000 characters per
    call; if truncated, call again with offset set to the next position."""
    if not url or not url.strip():
        return "ERROR: empty URL."
    url = url.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        video_id = url
    else:
        m = _YT_ID_RE.search(url)
        if not m:
            return f"ERROR: couldn't find a YouTube video id in '{url}'."
        video_id = m.group(1)
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return "ERROR: the youtube-transcript-api package isn't installed. Run: pip install youtube-transcript-api"

    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=["en", "en-GB", "en-US"]) if hasattr(api, "fetch") \
            else None
        if fetched is not None:
            lines = [snip.text if hasattr(snip, "text") else snip["text"] for snip in fetched]
        else:  # older package versions
            lines = [s["text"] for s in YouTubeTranscriptApi.get_transcript(video_id, languages=["en", "en-GB", "en-US"])]
    except Exception as e:  # the package raises many specific types; all mean "no transcript"
        return (f"ERROR: no transcript for video {video_id} - {type(e).__name__}: "
                f"{str(e).splitlines()[0] if str(e) else 'captions disabled, unavailable, or blocked'}. "
                f"Fall back to fetch_url/browse_url on the video page for its title and description.")

    title = ""
    try:
        oembed = ("https://www.youtube.com/oembed?format=json&url="
                  + urllib.parse.quote(f"https://www.youtube.com/watch?v={video_id}", safe=""))
        with urllib.request.urlopen(urllib.request.Request(oembed, headers={"User-Agent": USER_AGENT}), timeout=10) as r:
            meta = json.loads(r.read().decode("utf-8", errors="replace"))
        title = f"{meta.get('title', '')} - {meta.get('author_name', '')}"
    except Exception:
        pass

    text = " ".join(l.replace("\n", " ").strip() for l in lines if l and l.strip())
    total = len(text)
    chunk = text[offset:offset + MAX_CHARS]
    header = f"TRANSCRIPT of https://www.youtube.com/watch?v={video_id}" + (f"\nVIDEO: {title}" if title else "") + "\n\n"
    if not chunk:
        return header + f"(no text at offset {offset}; transcript is {total} characters long)"
    end = offset + len(chunk)
    footer = f"\n\n[TRUNCATED - showing characters {offset}-{end} of {total}. Call youtube_transcript again with offset={end} for the next part.]" if end < total else ""
    return header + chunk + footer
