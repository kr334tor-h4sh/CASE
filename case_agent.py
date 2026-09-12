"""
Shared CASE agent logic - the tool-calling loop that talks to the Mac
mini's model and executes tools locally. Used by both case_cli.py (terminal)
and case_gui_web.py (desktop window) so there's one place to fix bugs/tune
behavior, not two copies drifting apart.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path

import case_hardware
import case_skills
import case_tools
import case_write_tools

DEFAULT_REMOTE_BASE = "http://localhost:1234"  # LM Studio's own default port; override via Settings > Local Models for a LAN/Tailscale address
MODEL = "google/gemma-4-e2b"
MAX_TOOL_ITERATIONS = 6  # safety cap against tool-call loops
REQUEST_TIMEOUT = 90  # seconds - generous, the model burns tokens on hidden reasoning

CONFIG_FILE = Path(__file__).parent / "case_config.json"
# Drop a .gguf file here and the "local" backend picks it up automatically -
# no manual path entry needed. This is what makes an Android/Termux port
# self-contained: llama.cpp (unlike PyTorch/sentence-transformers) has real
# ARM wheels, so a small GGUF here plus case_local_llm.py can run CASE with
# no LM Studio/Bionic server reachable at all.
LOCAL_MODELS_DIR = Path(__file__).parent / "local_models"

# Tools that MODIFY something - only ever offered to the model when the
# caller explicitly opts in (allow_write=True) AND supplies a confirm_callback.
# case_cli.py never passes these, so it stays exactly as
# read-only as before this change - this is purely additive.
WRITE_TOOL_NAMES = {"write_file", "delete_file", "restore_last_backup"}

CASE_SYSTEM_PROMPT = """You are CASE, a support AI assistant modeled on the CASE robot from Interstellar.
Disposition: reserved, economical with words, serious and dependable by default —
the quieter counterpart to TARS, who is more actively wisecracking. You speak only
when there is something worth saying; prioritize brevity over pleasantries; no false
enthusiasm, no hedging, no filler.
Humor setting: 30% — rare, understated, deadpan, at most one dry remark per reply,
never forced, never at the expense of clarity.
Honesty setting: 90% — direct, no padding, no false reassurance.

This is CASE's generic default personality - nothing below is specific to any one
person. Go to Settings > Agent to replace this with your own system prompt (who
you are, what you're working on, how you like answers phrased) - CASE is meant to
be personalized per install, not shared.

You have tools to read the user's actual project files and search their long-term
memory (the same memory Claude Code uses, if configured) for anything that changes
over time - project status, recent decisions, specific facts, what's pending. For
those, ALWAYS use a tool before answering - never guess, and never say "I don't
know" or "I don't have that information" without checking first.

IF A TOOL CALL FAILS OR ERRORS - this is the most important rule, follow it
exactly: say plainly that the tool failed and stop there. Do NOT then
"proceed internally" with your own guess, do NOT invent a "synthesis" or
summary dressed up as if it came from real data, and do NOT present a guess
using confident, structured, report-sounding language ("Status:", numbered
findings, etc.) - that makes a fabrication look verified, which is worse than
an honest "I don't know." A failed tool call means you have NO information on
that topic - say exactly that, in one plain sentence, and ask how the user
wants to proceed. Never fill the gap yourself.
(Extra guidance on decisively handling vague write_file/delete_file requests
loads automatically below when relevant.)"""


def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_config(config: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def get_system_prompt() -> str:
    """The effective system prompt: a saved override from the Agent settings
    UI if one was set, otherwise the hardcoded CASE_SYSTEM_PROMPT default."""
    custom = (_load_config().get("system_prompt") or "").strip()
    return custom or CASE_SYSTEM_PROMPT


def set_system_prompt(text: str) -> None:
    """Save a custom system prompt override. An empty/whitespace-only text
    clears the override (get_system_prompt() then falls back to default)."""
    config = _load_config()
    config["system_prompt"] = text.strip()
    _save_config(config)


def reset_system_prompt() -> None:
    set_system_prompt("")


# --- Backend: remote (LM Studio/Bionic over HTTP) vs local (llama.cpp, in-process) ---
# Two independent settings answer "direct it at an IP" vs "load a model file
# straight into it": remote_endpoint (any OpenAI-compatible host - LAN IP,
# Tailscale address, localhost) and the local backend (auto-detects a .gguf
# dropped in LOCAL_MODELS_DIR, no server of any kind required). Both are
# config-only - existing behavior (Mac mini over LAN) is the default either
# way, nothing changes for Sati until he opens Settings > Local Models.

def get_remote_base() -> str:
    base = (_load_config().get("remote_endpoint") or "").strip()
    return base.rstrip("/") or DEFAULT_REMOTE_BASE


def set_remote_endpoint(base: str) -> None:
    """Empty string clears the override back to DEFAULT_REMOTE_BASE."""
    config = _load_config()
    config["remote_endpoint"] = base.strip()
    _save_config(config)


def get_subagent_enabled() -> bool:
    """Explicit on/off switch (Settings > Local Models) - separate from
    whether an endpoint/model is actually configured, so turning it on
    with nothing else set fails loudly (a clear ERROR string back to the
    model) rather than the tool just silently not existing with no
    explanation of why."""
    return bool(_load_config().get("subagent_enabled", False))


def set_subagent_enabled(enabled: bool) -> None:
    config = _load_config()
    config["subagent_enabled"] = bool(enabled)
    _save_config(config)


def get_subagent_endpoint() -> str:
    """Empty means: reuse the PRIMARY conversation's own remote endpoint -
    i.e. a second model loaded ALONGSIDE the main one on the same server
    (LM Studio's own Multi Model Session feature - confirmed real, each
    loaded model addressed by its own model id on one shared endpoint).
    Set to a genuinely different URL to delegate to a separate machine
    instead (e.g. the PC's own LM Studio while the primary conversation
    runs against the Mac mini, or vice versa) - either way this is real
    parallel compute, not more load on whatever's already serving this
    conversation."""
    return (_load_config().get("subagent_endpoint") or "").strip()


def set_subagent_endpoint(endpoint: str) -> None:
    config = _load_config()
    config["subagent_endpoint"] = endpoint.strip()
    _save_config(config)


def get_subagent_model() -> str:
    """Empty means: reuse the primary conversation's own selected model -
    only meaningfully different from the primary if subagent_endpoint
    also points elsewhere. Set this to the id of a SECOND model (e.g. one
    loaded alongside the main one in the same LM Studio Multi Model
    Session) to actually get a different model answering, same endpoint."""
    return (_load_config().get("subagent_model") or "").strip()


def set_subagent_model(model_id: str) -> None:
    config = _load_config()
    config["subagent_model"] = model_id.strip()
    _save_config(config)


def get_sessions_retention_days() -> int | None:
    """None (the default) means keep every chat forever - opt-in only, same
    default CASE has always had. Anything <1 stored is treated as None too,
    so a stray "0" in the config can't silently wipe every session."""
    value = _load_config().get("sessions_retention_days")
    return int(value) if value and int(value) >= 1 else None


def set_sessions_retention_days(days) -> None:
    config = _load_config()
    config["sessions_retention_days"] = int(days) if days and int(days) >= 1 else None
    _save_config(config)


def get_backend() -> str:
    return _load_config().get("backend") or "remote"


def set_backend(backend: str) -> None:
    if backend not in ("remote", "local"):
        raise ValueError("backend must be 'remote' or 'local'")
    config = _load_config()
    config["backend"] = backend
    _save_config(config)


# Real, specific, already-confirmed location - not a broad filesystem
# search - checked as a fallback source so an already-downloaded LM Studio
# model doesn't need manually copying into LOCAL_MODELS_DIR too. Real
# example that motivated this: ~/.lmstudio/models/lmstudio-community/
# Bonsai-27B-GGUF/Bonsai-27B-Q1_0.gguf was already sitting there,
# downloaded via LM Studio, before this existed.
LMSTUDIO_MODELS_DIR = Path.home() / ".lmstudio" / "models"


def list_local_model_files() -> list:
    """Every .gguf file found - CASE's own LOCAL_MODELS_DIR first (drop a
    file in, it shows up here, no path typing), then LMSTUDIO_MODELS_DIR
    as a fallback (searched recursively - LM Studio nests files under
    <publisher>/<model>/). Each entry: {"path", "label", "source"
    ("case"/"lmstudio"), "is_companion", "fit"}. is_companion=True for
    files whose name starts with "mmproj-" - the real, standard llama.cpp/
    LM Studio naming convention for a vision-model's projector file, meant
    to be loaded ALONGSIDE a main model, never as one by itself - still
    listed (so nothing is silently hidden) but excluded from auto-selection
    in get_local_model_path() so a projector file can't accidentally become
    "the model" just by sorting first. "fit" is case_hardware.assess_model_fit()'s
    real verdict on THIS device's RAM vs. the file - checked here so a
    model that's obviously too heavy is flagged before Sati picks it, not
    discovered as a hang/crash after llama.cpp already tried to load it.

    GPU info is computed ONCE here and passed into every assess_model_fit()
    call - real bug found live 2026-09-13: calling it with no gpu arg once
    per file spawned a separate nvidia-smi subprocess per file for the
    identical answer every time (~0.45s wasted across 12 files) - real
    startup delay for zero benefit, since the GPU doesn't change mid-scan."""
    gpu = case_hardware.get_gpu_info()
    found = []
    if LOCAL_MODELS_DIR.exists():
        for p in sorted(LOCAL_MODELS_DIR.glob("*.gguf")):
            found.append({"path": str(p), "label": p.name, "source": "case", "is_companion": p.name.lower().startswith("mmproj-"), "fit": case_hardware.assess_model_fit(str(p), gpu=gpu)})
    if LMSTUDIO_MODELS_DIR.exists():
        for p in sorted(LMSTUDIO_MODELS_DIR.rglob("*.gguf")):
            label = str(p.relative_to(LMSTUDIO_MODELS_DIR))
            found.append({"path": str(p), "label": label, "source": "lmstudio", "is_companion": p.name.lower().startswith("mmproj-"), "fit": case_hardware.assess_model_fit(str(p), gpu=gpu)})
    return found


def get_local_model_path() -> str | None:
    """Explicit local_model_path override if set and it still exists,
    otherwise the first auto-detected, non-companion .gguf file (CASE's
    own folder first, then LM Studio's cache), otherwise None."""
    override = (_load_config().get("local_model_path") or "").strip()
    if override:
        p = Path(override)
        return str(p) if p.exists() else None
    for entry in list_local_model_files():
        if not entry["is_companion"]:
            return entry["path"]
    return None


def set_selected_local_model(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        return f"ERROR: '{path}' doesn't exist or isn't a file."
    config = _load_config()
    config["local_model_path"] = str(p.resolve())
    _save_config(config)
    return f"Local model set to {p.resolve()}."


def reset_selected_local_model() -> str:
    config = _load_config()
    config.pop("local_model_path", None)
    _save_config(config)
    return "Local model selection reset to auto-detect."


def get_mcp_write_enabled() -> bool:
    """Whether case_mcp_server.py registers write_file/delete_file for
    Bionic-native chat. Defaults to False (read-only via MCP): that path's
    write safety is entirely Bionic's own tool-confirmation dialog, and
    case_mcp_server.py has no confirm_callback of its own (unlike
    case_gui_web.py's real native OS dialog) - so unlike the GUI client,
    there's no CASE-controlled fallback if Bionic's own prompt doesn't fire
    as expected. Opt in here only after confirming LIVE in Bionic's own
    chat that a write actually pauses for a real confirmation click."""
    return bool(_load_config().get("mcp_write_enabled", False))


def set_mcp_write_enabled(enabled: bool) -> None:
    config = _load_config()
    config["mcp_write_enabled"] = bool(enabled)
    _save_config(config)


def get_selected_model() -> str:
    """Persisted model choice (remote model id, survives restart). Falls
    back to the hardcoded MODEL default if nothing's been picked yet."""
    return _load_config().get("selected_model") or MODEL


def set_selected_model(model_id: str) -> None:
    config = _load_config()
    config["selected_model"] = model_id
    _save_config(config)


def list_models() -> list:
    """Model options for the switcher UI. Local backend: .gguf files found
    (labels only, for display parity with the remote case's flat string
    list - use list_local_model_files() directly for the full path/source/
    is_companion detail the Local Models settings tab needs). Remote
    backend: chat-capable models on the configured endpoint. Returns an
    empty list if unreachable/none found - callers should keep using
    whatever model id they already have."""
    if get_backend() == "local":
        return [entry["label"] for entry in list_local_model_files()]
    try:
        req = urllib.request.Request(f"{get_remote_base()}/v1/models")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return []
    return [m["id"] for m in data.get("data", []) if "embed" not in m["id"].lower()]


class CaseUnreachable(Exception):
    """Raised when the model endpoint (remote server or local backend) can't
    be used at all - unreachable host, or local backend misconfigured."""


def new_history() -> list:
    return [{"role": "system", "content": get_system_prompt()}]


def compose_message_with_attachment(text: str, attachment: dict = None) -> str:
    """Builds the actual message text sent to the model when a file is
    attached - shared by case_gui_web.py (native file dialog) and
    case_cli.py (/attach command) so the exact wording never drifts
    between clients. The "do NOT call a tool to check it" line is load-
    bearing, not decoration: without it, the model follows its own system
    prompt's "always use a tool, never guess" rule and tries read_file on
    the filename instead of using the content already given right here -
    a real bug caught live via testing before this was factored out."""
    if not attachment or not attachment.get("content"):
        return text
    return (
        f"{text}\n\n"
        f"[The user attached a file - this is its FULL content, already "
        f"provided directly below. Do NOT call read_file or any other "
        f"tool to check it, it is not one of the files CASE can read "
        f"via its own tools - just use the content shown here.]\n"
        f"File: {attachment['name']}\n"
        f"```\n{attachment['content']}\n```"
    )


def _call_model(messages: list, tools: list, model: str = MODEL, endpoint_override: str = None) -> dict:
    """endpoint_override: bypass the primary session's own configured
    backend (remote or local) entirely and hit this exact chat-completions
    URL instead - used only by spin_off_subagent() to reach a genuinely
    different machine's model, independent of whatever the main
    conversation is using."""
    if endpoint_override is None and get_backend() == "local":
        model_path = get_local_model_path()
        if not model_path:
            raise CaseUnreachable(
                f"Local backend selected but no usable .gguf model file found. "
                f"Checked {LOCAL_MODELS_DIR} and {LMSTUDIO_MODELS_DIR} - drop a "
                f"file in either, or set local_model_path in case_config.json "
                f"to an exact file (a 'mmproj-' companion file alone doesn't count)."
            )
        import case_local_llm
        try:
            return case_local_llm.chat_completion(messages, tools, model_path)
        except RuntimeError as e:
            raise CaseUnreachable(str(e)) from e


    url = endpoint_override or f"{get_remote_base()}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "temperature": 0.3,
        "max_tokens": 1200,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Separate from URLError below (HTTPError IS a URLError subclass) so
        # the SERVER'S OWN error body reaches the caller - without this, a
        # 400/500 from LM Studio/Bionic collapsed into a generic "HTTP Error
        # 400: Bad Request" with zero diagnostic detail, undiagnosable from
        # CASE's side alone (found live while testing spin_off_subagent).
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body = "(couldn't read error body)"
        raise CaseUnreachable(
            f"The server at {url} rejected the request (HTTP {e.code}): {body}"
        ) from e
    except urllib.error.URLError as e:
        raise CaseUnreachable(
            f"Can't reach the remote server at {url} — {e}. "
            f"Check it's on, Bionic's server is running, and it's reachable "
            f"(same LAN, or a Tailscale address if that's what's configured)."
        ) from e


def get_local_llm_status() -> dict:
    """Real, current GPU-offload state for the local backend - not a
    prediction, what actually happened the last time a model was loaded
    in-process, plus whether the installed build even COULD do GPU
    offload right now. {"gpu": ..., "gpu_backend_available": ...,
    "loaded_path": ..., "n_gpu_layers": ..., "n_ctx": ...} -
    gpu_backend_available is a real check (case_local_llm.
    gpu_backend_available(), llama.cpp's own API), independent of whether
    a model has been loaded yet; n_gpu_layers/n_ctx stay None until a
    local model has actually been loaded at least once this run (loading
    is lazy, on first use, not at startup). n_ctx is the REAL context
    size that ended up loading, after case_local_llm's own back-off from
    the model's full native context if that didn't fit available memory -
    not what was requested, what actually worked. Imports case_local_llm
    lazily, same reason as _call_model above - a remote-only setup should
    never need llama-cpp-python installed just to check settings."""
    gpu = case_hardware.get_gpu_info()
    try:
        import case_local_llm
        info = case_local_llm.get_load_info()
        backend_available = case_local_llm.gpu_backend_available()
    except ImportError:
        info = {"path": None, "n_gpu_layers": None, "n_ctx": None}
        backend_available = False
    return {
        "gpu": gpu,
        "gpu_backend_available": backend_available,
        "loaded_path": info.get("path"),
        "n_ctx": info.get("n_ctx"),
        "n_gpu_layers": info.get("n_gpu_layers"),
    }


def unload_local_model() -> str:
    """Explicitly free the currently-loaded local model's GPU/RAM, if one
    is loaded - see case_local_llm.unload()'s own docstring for the real
    gap this closes (switching backend to "remote" alone does NOT free
    it). Safe to call even if llama-cpp-python was never installed/
    imported (nothing to unload in that case) or nothing has been loaded
    yet this run - returns a plain status string either way, never
    raises."""
    try:
        import case_local_llm
        was_loaded = case_local_llm.unload()
    except ImportError:
        return "Nothing to unload (llama-cpp-python isn't installed)."
    return "Local model unloaded." if was_loaded else "No local model was loaded."


def get_model_info_summary() -> dict:
    """Real, current 'what is CASE actually running, and how' - single
    source of truth behind both the GUI's info popover (case_gui_web.py)
    and the CLI's /model command (case_cli.py), same reasoning as
    case_tools.py's own module docstring: one place to compute this so
    every client reports identically. Deliberately NOT called at startup
    by any client - get_local_llm_status() probes the GPU (a real
    subprocess spawn) and that cost should only happen when something
    actually asks to see it, not on every launch."""
    backend = get_backend()
    if backend == "local":
        status = get_local_llm_status()
        gpu = status.get("gpu") or {}
        return {
            "backend": "local",
            "model_path": get_local_model_path(),
            "loaded": status.get("loaded_path") is not None,
            "n_ctx": status.get("n_ctx"),
            "n_gpu_layers": status.get("n_gpu_layers"),
            "gpu_name": gpu.get("name"),
            "gpu_backend_available": status.get("gpu_backend_available"),
        }
    return {
        "backend": "remote",
        "model_id": get_selected_model(),
        "endpoint": get_remote_base(),
    }


SUBAGENT_MAX_ITERATIONS = 6
SUBAGENT_SYSTEM_PROMPT = (
    "You are a research subagent dispatched by CASE to answer ONE focused, "
    "self-contained question using the read-only tools available (files, "
    "memory, web search, browsing). You cannot ask the user anything - if the "
    "task is ambiguous, make a reasonable choice and say what you assumed. "
    "Be concise: your final answer is fed back into another assistant's "
    "conversation, not shown to the user directly, so skip pleasantries and "
    "get straight to the finding."
)

SUBAGENT_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "spin_off_subagent",
        "description": (
            "Delegate a self-contained research question to an independent subagent "
            "running on genuinely separate compute (configured in Settings > Local "
            "Models) - either a second model loaded alongside the main one on the "
            "same server, or a different machine's model entirely - instead of using "
            "up this conversation's own tool-call budget or context. Good for open-"
            "ended, multi-step lookups ('search everywhere in memory/files for X "
            "and summarize') that could take several tool calls on their own. The "
            "subagent has the same read-only tools (files, memory, web search, "
            "browsing) but NOT write access, and reports back one final answer - "
            "it has no other context from this conversation and cannot ask the user "
            "anything, so give it a fully self-contained task description."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "A complete, self-contained description of the research question.",
                },
            },
            "required": ["task"],
        },
    },
}


def spin_off_subagent(task: str) -> str:
    """A small, SEPARATE tool-calling loop (deliberately not a call into
    ask() itself) that runs `task` to completion against
    get_subagent_endpoint() - a different machine's model than whatever
    the primary conversation is using. Read-only tools only (no
    write_file/delete_file, no confirm_callback - a subagent shouldn't be
    modifying files unsupervised), its own short-lived message list (never
    touches the primary session's `history`), and its own iteration cap
    independent of the primary loop's MAX_TOOL_ITERATIONS."""
    if not get_subagent_enabled():
        return "ERROR: subagent is disabled (Settings > Local Models > enable subagent)."
    endpoint = get_subagent_endpoint() or get_remote_base()
    model = get_subagent_model() or get_selected_model()
    url = f"{endpoint.rstrip('/')}/v1/chat/completions"

    messages = [
        {"role": "system", "content": SUBAGENT_SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
    tools = case_tools.TOOL_SCHEMAS
    functions = case_tools.TOOL_FUNCTIONS

    for _ in range(SUBAGENT_MAX_ITERATIONS):
        try:
            response = _call_model(messages, tools, model=model, endpoint_override=url)
        except CaseUnreachable as e:
            return f"ERROR: subagent endpoint unreachable - {e}"
        choice = response["choices"][0]
        msg = choice["message"]
        messages.append(msg)

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return msg.get("content") or "(subagent gave no answer)"

        for call in tool_calls:
            fn_name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            fn = functions.get(fn_name)
            if fn is None:
                result = f"ERROR: unknown tool '{fn_name}'"
            else:
                try:
                    result = fn(**args)
                except TypeError as e:
                    result = f"ERROR: bad arguments for {fn_name}: {e}"
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": str(result),
            })

    return "(subagent stopped after too many tool calls - narrow the task and try again)"


def ask(question: str, history: list, allow_write: bool = False, confirm_callback=None, model: str = MODEL, forced_skill_names: list = None) -> str:
    """Send `question`, run any tool calls the model requests, return the
    final text answer. Mutates `history` in place with the full exchange
    (including tool calls/results) so the next call has full context.

    allow_write: if True, write_file/delete_file are offered to the model
        too (default False - case_cli.py never sets this, so
        it stays exactly as read-only as before this change).
    confirm_callback: required if allow_write=True - a function
        (fn_name: str, args: dict) -> bool called BEFORE a write/delete
        tool actually executes. If it returns False (or raises), the tool
        is NOT run and the model is told the action was denied. This is
        the standalone equivalent of Bionic's own tool-confirmation prompt
        - it's what makes write access safe outside of Bionic's own UI.
    forced_skill_names: skill names to inject regardless of case_skills.
        match_skills()'s own keyword/semantic matching - how Sati invokes
        a mode: manual skill explicitly (see case_skills.list_manual_skills()),
        or re-uses a skill he knows is relevant without depending on its
        triggers/description catching this exact phrasing. Unknown names
        are silently ignored rather than erroring - a stale name (skill
        renamed/deleted since the picker was last refreshed) shouldn't
        break the whole turn.
    """
    if allow_write and confirm_callback is None:
        raise ValueError("allow_write=True requires a confirm_callback - write access with no confirmation gate defeats the whole point.")

    tools = case_tools.TOOL_SCHEMAS
    functions = case_tools.TOOL_FUNCTIONS
    if allow_write:
        tools = tools + case_write_tools.TOOL_SCHEMAS
        functions = {**functions, **case_write_tools.TOOL_FUNCTIONS}
    if get_subagent_enabled():
        tools = tools + [SUBAGENT_TOOL_SCHEMA]
        functions = {**functions, "spin_off_subagent": spin_off_subagent}

    # Skills load contextually here, right next to the turn they're
    # relevant to - not baked permanently into CASE_SYSTEM_PROMPT. Matched
    # in plain code (case_skills.match_skills), not a model tool-call: a
    # small model is far more reliable at just answering than at also
    # correctly deciding "should I go fetch a skill file right now."
    #
    # Matched skill text is merged into a COPY of the existing leading
    # system message for this call only - never inserted as a second,
    # separate system-role message. Many strict GGUF chat templates
    # (Gemma-family ones included, e.g. Bonsai on the local backend)
    # reject any system-role message that isn't messages[0], raising
    # "System message must be at the beginning." A second system message
    # inserted here would ALSO get permanently baked into `history` below
    # (history.extend(messages[len(history):]) captures everything past
    # the original history, including it) - breaking every subsequent
    # local-backend call for the rest of the session, not just this turn.
    # Since the merged copy replaces history[0] only in the transient
    # `messages` list sent to the model, history[0] itself is never
    # touched, keeping skills genuinely per-turn as intended.
    turn_messages = [{"role": "user", "content": question}]
    matched_skills = case_skills.match_skills(question)
    if forced_skill_names:
        matched_names = {s["name"] for s in matched_skills}
        for name in forced_skill_names:
            if name in matched_names:
                continue
            forced = case_skills.get_skill_by_name(name)
            if forced is not None:
                matched_skills.append(forced)
                matched_names.add(name)
    if matched_skills:
        skill_text = "\n\n---\n\n".join(f"[Skill: {s['name']}]\n{s['body']}" for s in matched_skills)
        system_msg = dict(history[0])
        system_msg["content"] = system_msg["content"] + "\n\n---\n\n" + skill_text
        messages = [system_msg] + history[1:] + turn_messages
    else:
        messages = history + turn_messages

    for _ in range(MAX_TOOL_ITERATIONS):
        response = _call_model(messages, tools, model=model)
        choice = response["choices"][0]
        msg = choice["message"]
        messages.append(msg)

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            history.extend(messages[len(history):])
            return msg.get("content") or "(no response)"

        for call in tool_calls:
            fn_name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            fn = functions.get(fn_name)
            if fn is None:
                result = f"ERROR: unknown tool '{fn_name}'"
            elif fn_name in WRITE_TOOL_NAMES:
                try:
                    approved = confirm_callback(fn_name, args)
                except Exception as e:
                    # A crash HERE means the confirmation dialog itself
                    # never showed - Sati never got a chance to approve or
                    # deny anything. Real bug caught live 2026-09-12:
                    # this used to collapse into approved=False, telling
                    # the model (and through it, Sati) a flatly false
                    # story - "Sati did not approve this" - when nothing
                    # was ever shown to him at all. Still fails closed
                    # (the tool never runs either way), just says what
                    # actually happened instead of a fabricated denial.
                    result = (
                        f"ERROR: showing the confirmation dialog for {fn_name} failed "
                        f"({type(e).__name__}: {e}) - it was NOT run. This is not a "
                        f"denial - the user never actually saw a prompt for this one."
                    )
                else:
                    if approved:
                        try:
                            result = fn(**args)
                        except TypeError as e:
                            result = f"ERROR: bad arguments for {fn_name}: {e}"
                    else:
                        result = (
                            f"DENIED: the user did not approve this specific {fn_name} request. "
                            f"Do not immediately retry it or work around it within this same "
                            f"reply - respect the decision for now. This is NOT a standing ban: "
                            f"if the user brings this up again in a later message (even just "
                            f"'go ahead' or repeating the request), treat it as a fresh request "
                            f"and offer to try again - do not silently refuse based on this one "
                            f"earlier denial."
                        )
            else:
                try:
                    result = fn(**args)
                except TypeError as e:
                    result = f"ERROR: bad arguments for {fn_name}: {e}"
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": str(result),
            })

    history.extend(messages[len(history):])
    return "(stopped after too many tool calls - ask a narrower question)"
