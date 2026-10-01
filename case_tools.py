"""
Shared tool implementations for CASE - read-only access to Sati's project
files, memory notes, and the memory-reflect semantic search. Used by both
case_mcp_server.py (Bionic's own MCP integration, local stdio) and
case_cli.py (the PC-side agent loop that calls the Mac mini's model).

Single source of truth so both entry points behave identically.
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from case_web_search import web_search
from case_browser import browse_url, open_browser_window
from case_fetch import fetch_url, youtube_transcript

# When set, list_directory/read_file/search_files/memory_reflect proxy over
# HTTP to case_file_server.py running on whichever machine actually has the
# real project/memory files (normally this PC), instead of touching the
# local filesystem at all - the missing piece for a client with NO local
# copy of Sati's files (e.g. a phone). Empty by default: nothing changes
# for the existing PC clients, which never set this.
FILE_SERVER_URL = os.environ.get("CASE_FILE_SERVER_URL", "").strip()


def _remote_call(endpoint: str, **params) -> str:
    url = f"{FILE_SERVER_URL.rstrip('/')}/{endpoint}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("result", "ERROR: malformed response from CASE file server")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return f"ERROR: couldn't reach the CASE file server at {FILE_SERVER_URL} - {e}"


# Workspace roots are real runtime settings now (Settings > General on the
# GUI, same case_config.json every client reads), not fixed at import time -
# a small local config reader/writer duplicated here (rather than imported
# from case_agent.py) specifically to avoid a circular import, since
# case_agent.py itself imports this module. Precedence for each:
# case_config.json's own key > the CASE_* env var > the real hardcoded
# default. The env var stays the mechanism for Android/Termux, which has
# no local Settings UI to change this from.
_CONFIG_FILE = Path(__file__).parent / "case_config.json"


def _load_local_config() -> dict:
    if not _CONFIG_FILE.exists():
        return {}
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_local_config(config: dict) -> None:
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def get_project_root() -> Path:
    override = (_load_local_config().get("project_root") or "").strip()
    if override:
        return Path(override).resolve()
    # Home directory is the only default that's actually valid on every
    # machine - set your real project folder via Settings > General
    # (project_root) or the CASE_PROJECT_ROOT env var (used on Android/
    # Termux, which has no Settings UI to change this from).
    return Path(os.environ.get("CASE_PROJECT_ROOT", str(Path.home()))).resolve()


def get_memory_root() -> Path:
    override = (_load_local_config().get("memory_root") or "").strip()
    if override:
        return Path(override).resolve()
    return Path(os.environ.get(
        "CASE_MEMORY_ROOT",
        str(Path.home() / ".claude"),
    )).resolve()


def get_allowed_roots() -> list:
    return [get_project_root(), get_memory_root()]


def get_memory_db() -> Path:
    return get_memory_root() / "memory.db"


def set_project_root(path: str) -> str:
    p = Path(path)
    if not p.is_dir():
        return f"ERROR: '{path}' doesn't exist or isn't a folder."
    config = _load_local_config()
    config["project_root"] = str(p.resolve())
    _save_local_config(config)
    return f"Workspace folder set to {p.resolve()}."


def reset_project_root() -> str:
    config = _load_local_config()
    config.pop("project_root", None)
    _save_local_config(config)
    return "Workspace folder reset to default."


def set_memory_root(path: str) -> str:
    p = Path(path)
    if not p.is_dir():
        return f"ERROR: '{path}' doesn't exist or isn't a folder."
    config = _load_local_config()
    config["memory_root"] = str(p.resolve())
    _save_local_config(config)
    return f"Memory folder set to {p.resolve()}."


def reset_memory_root() -> str:
    config = _load_local_config()
    config.pop("memory_root", None)
    _save_local_config(config)
    return "Memory folder reset to default."


MEMORY_ENGINE = Path(os.environ.get("CASE_MEMORY_ENGINE", str(Path.home() / ".claude" / "memory_engine.py")))
PYTHON_EXE = Path(os.environ.get(
    "CASE_PYTHON_EXE",
    sys.executable,  # whatever interpreter is running CASE right now - always valid, unlike a hardcoded path
))

MAX_FILE_BYTES = 200_000
MAX_SEARCH_RESULTS = 40
MAX_LIST_ENTRIES = 500


def _resolve_safe(path_str: str) -> Path:
    project_root = get_project_root()
    allowed_roots = get_allowed_roots()
    if not path_str or path_str.strip() in (".", "/", "\\"):
        return project_root
    candidate = Path(path_str)
    if not candidate.is_absolute():
        for root in allowed_roots:
            resolved = (root / candidate).resolve()
            try:
                resolved.relative_to(root)
                if resolved.exists():
                    return resolved
            except ValueError:
                continue
        return (project_root / candidate).resolve()
    resolved = candidate.resolve()
    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise PermissionError(
        f"'{path_str}' is outside the allowed folders ({project_root} or {get_memory_root()})."
    )


def list_directory(path: str = "") -> str:
    if FILE_SERVER_URL:
        return _remote_call("list_directory", path=path)
    try:
        target = _resolve_safe(path)
    except PermissionError as e:
        return f"ERROR: {e}"
    if not target.exists():
        return f"ERROR: '{path}' does not exist."
    if target.is_file():
        return f"'{path}' is a file, not a directory. Use read_file instead."
    entries = []
    try:
        for i, entry in enumerate(sorted(target.iterdir())):
            if i >= MAX_LIST_ENTRIES:
                entries.append(f"... (truncated at {MAX_LIST_ENTRIES} entries)")
                break
            kind = "DIR " if entry.is_dir() else "FILE"
            size = "" if entry.is_dir() else f" ({entry.stat().st_size:,} bytes)"
            entries.append(f"{kind}  {entry.name}{size}")
    except PermissionError:
        return f"ERROR: permission denied reading '{path}'."
    return f"Contents of {target}:\n" + "\n".join(entries) if entries else "(empty directory)"


def read_file(path: str) -> str:
    if FILE_SERVER_URL:
        return _remote_call("read_file", path=path)
    try:
        target = _resolve_safe(path)
    except PermissionError as e:
        return f"ERROR: {e}"
    if not target.exists():
        return f"ERROR: '{path}' does not exist."
    if target.is_dir():
        return f"ERROR: '{path}' is a directory. Use list_directory instead."
    try:
        data = target.read_bytes()
    except PermissionError:
        return f"ERROR: permission denied reading '{path}'."
    if b"\x00" in data[:1000]:
        return (
            f"ERROR: '{path}' looks like a binary file (docx/pdf/xlsx/exe/zip "
            f"etc) - not readable as text."
        )
    truncated = len(data) > MAX_FILE_BYTES
    text = data[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
    if truncated:
        text += f"\n\n... (truncated, file is {len(data):,} bytes, showing first {MAX_FILE_BYTES:,})"
    return text


MAX_ATTACH_BYTES = 100_000


def read_file_for_attach(path: str) -> dict:
    """Read a file Sati picked himself via a native OS file dialog, for the
    GUI's Attach button - NOT a model tool (not in TOOL_SCHEMAS/TOOL_FUNCTIONS
    below, never callable by the model). Deliberately has no ALLOWED_ROOTS
    restriction: that guard exists to stop a hallucinating/malicious model
    from reading arbitrary files, but here Sati already chose the exact file
    through the OS's own picker - the OS dialog itself is the access control,
    same trust boundary as any other file-open dialog in any app.
    Returns {"name", "content"} on success or {"name", "error"} on failure -
    never raises, so the GUI can show the error inline."""
    target = Path(path)
    name = target.name
    if not target.exists():
        return {"name": name, "error": "File no longer exists."}
    if target.is_dir():
        return {"name": name, "error": "That's a folder, not a file."}
    try:
        data = target.read_bytes()
    except (PermissionError, OSError) as e:
        return {"name": name, "error": f"Couldn't read it: {e}"}
    if b"\x00" in data[:1000]:
        return {"name": name, "error": "Looks like a binary file (image/pdf/docx/exe etc) - only text/code files can be attached."}
    truncated = len(data) > MAX_ATTACH_BYTES
    text = data[:MAX_ATTACH_BYTES].decode("utf-8", errors="replace")
    if truncated:
        text += f"\n\n... (truncated, file is {len(data):,} bytes, showing first {MAX_ATTACH_BYTES:,})"
    return {"name": name, "content": text}


def search_files(query: str, path: str = "") -> str:
    if FILE_SERVER_URL:
        return _remote_call("search_files", query=query, path=path)
    try:
        target = _resolve_safe(path)
    except PermissionError as e:
        return f"ERROR: {e}"
    if not target.exists():
        return f"ERROR: '{path}' does not exist."
    query_lower = query.lower()
    results = []
    skip_dirs = {".git", "venv", "__pycache__", "node_modules", ".lmstudio", "output", "case_backups", "case_sessions", "local_models"}
    skip_exts = {".exe", ".zip", ".pdf", ".docx", ".xlsx", ".pyc", ".dll", ".jpg", ".jpeg", ".png"}
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            if len(results) >= MAX_SEARCH_RESULTS:
                break
            fpath = Path(root) / fname
            if fpath.suffix.lower() in skip_exts:
                if query_lower in fname.lower():
                    results.append(f"{fpath} (filename match)")
                continue
            if query_lower in fname.lower():
                results.append(f"{fpath} (filename match)")
                continue
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    for lineno, line in enumerate(f, 1):
                        if query_lower in line.lower():
                            results.append(f"{fpath}:{lineno}: {line.strip()[:200]}")
                            break
            except (PermissionError, OSError):
                continue
        if len(results) >= MAX_SEARCH_RESULTS:
            break
    if not results:
        return f"No matches for '{query}' under {target}."
    header = f"Matches for '{query}' under {target}"
    if len(results) >= MAX_SEARCH_RESULTS:
        header += f" (showing first {MAX_SEARCH_RESULTS})"
    return header + ":\n" + "\n".join(results)


def memory_reflect(query: str) -> str:
    if FILE_SERVER_URL:
        return _remote_call("memory_reflect", query=query)
    if not MEMORY_ENGINE.exists():
        return f"ERROR: memory engine not found at {MEMORY_ENGINE}."
    env = os.environ.copy()
    env["MEMORY_DB_PATH"] = str(get_memory_db())
    # CASE's GUI runs as pythonw.exe (no console of its own) - spawning the
    # console-subsystem python.exe here without CREATE_NO_WINDOW makes
    # Windows briefly allocate and flash a console window for the child on
    # every single call (real, confirmed 2026-09-12 - this is the actual
    # cause behind the recurring "the window flickers" reports, not GPU
    # contention as originally guessed).
    kwargs = dict(
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env=env,
    )
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            [str(PYTHON_EXE), str(MEMORY_ENGINE), "reflect-prep", query],
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        return "ERROR: memory reflect timed out after 60s."
    if result.returncode != 0:
        return f"ERROR running memory_engine.py: {result.stderr[-2000:]}"
    output = result.stdout.strip()
    if len(output) > 15_000:
        output = output[:15_000] + "\n... (truncated)"
    return output or "(no output)"


# OpenAI-style tool schemas, shared by case_cli.py for the /v1/chat/completions
# `tools` parameter.
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and subfolders under a directory. Pass an empty string to list the root project folder.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative or absolute path, must stay inside the allowed project/memory folders."}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the text content of a specific file (markdown, .py, .txt, .json etc). Binary files (docx/pdf/xlsx) are rejected.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Case-insensitive text search across files under a folder (defaults to the whole project root). Returns matching file paths with a snippet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_reflect",
            "description": "Semantically search the user's long-term memory (same memory Claude Code uses, if configured) for anything relevant to a topic - project status, past decisions, preferences, facts.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the live web (no API key, independent of any other service). Use for current events, recent news, or anything outside the user's own project files/memory. Occasionally rate-limited by the search backend if called too rapidly in a row - if that happens, say so plainly and suggest trying again shortly, do not report it as 'no results exist.'",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browse_url",
            "description": "Open a REAL, visible browser window showing a specific URL (stays open on screen for the user to look at/use directly) and return the page's actual text content. Use when a specific page needs to actually be seen/read in full, not for a quick search - use web_search first to find the right URL, then browse_url to actually open and read it. IMPORTANT when reading the result: the page's OWN primary content (title, description, main text) appears FIRST in the extracted text - trailing content after that (e.g. a 'related videos'/'recommended'/'you might also like' list, sidebar links) is NOT the page you were asked about, it's just navigational clutter alongside it. Answer about the primary content specifically; don't lump it into one undifferentiated list with the recommendations that follow it.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_browser_window",
            "description": "Open a plain, persistent, interactive browser window for the user to use directly (defaults to Google if no URL given) - for when they want to search/browse themselves like a normal browser tab, not for CASE to read a page's content. Use this instead of browse_url when the user asks to open a browser to search/look around themselves, rather than asking CASE to fetch and summarize one specific page.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Optional starting URL - omit or leave empty to default to Google."}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch a URL with a plain HTTP GET and return its readable text - NO browser window opens. Works for static pages, articles, RSS feeds and JSON APIs (e.g. SEC EDGAR). Returns up to 20,000 characters per call; if the result says TRUNCATED, call again with offset set to the number it gives. It cannot run JavaScript: if the text comes back empty or just navigation boilerplate, the site renders client-side - use browse_url for it instead. Prefer this over browse_url whenever the page is static, because it is faster and does not pop a window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "offset": {"type": "integer", "description": "Character position to start from, for reading the next part of a long page. Default 0."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "youtube_transcript",
            "description": "Get the spoken transcript of ONE specific YouTube video from its link (or 11-character id), plus its title. Returns up to 20,000 characters per call; if TRUNCATED, call again with offset. Not for channel/playlist episode lists - those need browse_url. If it reports no transcript, say so plainly rather than guessing the video's content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "offset": {"type": "integer", "description": "Character position to start from. Default 0."},
                },
                "required": ["url"],
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "list_directory": list_directory,
    "read_file": read_file,
    "search_files": search_files,
    "memory_reflect": memory_reflect,
    "web_search": web_search,
    "browse_url": browse_url,
    "open_browser_window": open_browser_window,
    "fetch_url": fetch_url,
    "youtube_transcript": youtube_transcript,
}
