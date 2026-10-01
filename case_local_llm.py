"""
Local, in-process LLM inference via llama.cpp (llama-cpp-python) - the
"load a model file straight into it" backend, as opposed to case_agent.py's
default of calling a remote LM Studio/Bionic server over HTTP.

Why this exists: on Android/Termux there is no LM Studio/Bionic to call at
all. llama.cpp has real ARM/Termux wheels (unlike PyTorch/sentence-
transformers, ruled out separately for memory_reflect - see the
local-ai-app-guide skill's Android section), so a small .gguf file dropped
into CASE/local_models/ plus this module can run CASE fully standalone,
no server or network reachability required. On the PC this is mostly
useful for testing that path before an actual Android port, since LM
Studio already does this job well when a server IS available.

llama-cpp-python is an OPTIONAL dependency - only imported the first time
the local backend is actually selected and used, so a remote-only setup
(the default) never needs it installed.

GPU offload (hybrid RAM+VRAM): when a GPU is detected (case_hardware.py),
loading tries progressively less-aggressive n_gpu_layers values, falling
back to full CPU-only if every GPU attempt fails. This is deliberately a
real load-and-back-off strategy, not a precise "N layers = X bytes"
calculation - llama.cpp doesn't expose a model's real per-layer VRAM cost
without loading it first (total file size isn't evenly split across
layers, and KV cache/compute buffers add more on top), so a formula here
would be false precision. n_gpu_layers=-1 means "offload every layer" -
llama.cpp puts as many layers as fit in VRAM and keeps the rest on CPU
automatically (this IS "hybrid RAM+VRAM": most models that don't fully
fit still run partially offloaded rather than all-or-nothing) - the
back-off only exists for the case where even that partial split still
doesn't fit and llama.cpp raises rather than degrading further itself. A
CPU-only llama-cpp-python build (no CUDA compiled in) accepts these same
n_gpu_layers values without error and simply runs on CPU regardless -
still correct, just not distinguishable from "chose not to" after the
fact, which is why get_load_info() reports what was actually requested,
not a confirmed measurement.

Two independent things both have to be true for real GPU offload to do
anything: (1) the hardware supports it - case_hardware.get_gpu_info(),
and (2) the INSTALLED llama-cpp-python build was actually compiled with
a GPU backend - gpu_backend_available() below. (1) alone isn't enough:
detecting an Adreno chip on Android is easy, but a GGML_OPENCL-enabled
build is a real, separate, non-default step (see ANDROID_SETUP.md) that
(1) has no way to see. Gating attempts on both means a plain CPU-only
build (the common case until someone deliberately rebuilds) skips
straight to n_gpu_layers=0 - no wasted attempt, no misleading "tried and
silently did nothing" ambiguity in get_load_info()'s output.
"""

import json
import re

import case_hardware

# Real bug, confirmed live 2026-09-13 via an actual failed write_gating
# regression check: raw llama-cpp-python has no chat_format registered for
# Qwen2.5 (or several other model families that share this same
# convention - Hermes-style function calling), so a tool call comes back
# as literal text in `content` instead of a structured tool_calls array:
#   <tool_call>
#   {"name": "write_file", "arguments": {"path": "...", ...}}
#   </tool_call>
# LM Studio's server parses this into OpenAI-shaped tool_calls
# automatically; raw llama-cpp-python does not. Without this, CASE's
# entire write/delete confirmation gate silently never fires locally -
# confirm_callback is never even called, so a "denied" write looks
# identical to an "approved" one from the model's perspective (neither
# actually happens, but for the wrong reason - the tool call was never
# recognized as one at all). This is CASE's own equivalent of the
# server-side parser LM Studio has - narrow (this one documented tag
# format), not a general chat-template engine.
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


# Second tag convention, confirmed live 2026-10-01 with Qwen3.5-9B: XML-ish
# instead of JSON. Its calls came back as the visible ANSWER text and never
# ran, because the JSON-only parser below didn't recognise them:
#   <tool_call>
#   <function=web_search>
#   <parameter=query>
#   Bitcoin price today
#   </parameter>
#   </function>
#   </tool_call>
_XML_CALL_RE = re.compile(r"<tool_call>\s*<function=([^>\s]+)>(.*?)</function>\s*</tool_call>", re.DOTALL)
_XML_PARAM_RE = re.compile(r"<parameter=([^>\s]+)>\s*(.*?)\s*</parameter>", re.DOTALL)


def _coerce_xml_value(raw: str):
    """XML parameters are all text. Keep strings as strings, but turn a
    value that is clearly a JSON number/bool/object back into one so a
    tool taking an integer (e.g. offset) receives an integer."""
    stripped = raw.strip()
    if stripped and (stripped[0] in "{[" or stripped in ("true", "false", "null") or re.fullmatch(r"-?\d+(\.\d+)?", stripped)):
        try:
            return json.loads(stripped)
        except ValueError:
            pass
    return stripped


def _parse_raw_tool_calls(content: str):
    """Real, narrow parser for the two raw tool-call tag formats seen so
    far: <tool_call>{json}</tool_call> (Qwen2.5/Hermes) and the XML-style
    <tool_call><function=name><parameter=k>v</parameter></function></tool_call>
    (Qwen3.5). Returns (remaining_content, tool_calls_list) - the list is
    empty if no valid block was found (the normal case for any model that
    isn't trying to call a tool), in which case remaining_content is the
    original text, untouched. A block that fails to parse, or lacks a
    name, is skipped rather than crashing the whole response - one
    malformed call shouldn't take down a response that might have other
    valid content."""
    tool_calls = []
    for m in _TOOL_CALL_RE.finditer(content):
        try:
            data = json.loads(m.group(1))
            name = data["name"]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        # Real, live-observed inconsistency (2026-09-12): Qwen2.5 doesn't
        # commit to one convention for "arguments" - sometimes a nested
        # JSON object (Hermes-style function calling), sometimes an
        # already-JSON-encoded STRING (the actual OpenAI wire format for
        # tool_calls[].function.arguments, which Qwen was also plainly
        # trained on). Blindly re-json.dumps()-ing a value that's already
        # a string double-encodes it into unparseable garbage - this is
        # exactly what silently broke restore_last_backup's confirmation
        # dialog live in the GUI (case_agent.ask()'s confirm_callback
        # crashed on args.get() against a string, got swallowed into a
        # false "Sati denied it" with no dialog ever shown - see the fix
        # there too). Only re-encode when it's genuinely a dict.
        raw_args = data.get("arguments")
        args_json = raw_args if isinstance(raw_args, str) else json.dumps(raw_args or {})
        tool_calls.append({
            "id": f"call_{len(tool_calls)}",
            "type": "function",
            "function": {"name": name, "arguments": args_json},
        })
    for m in _XML_CALL_RE.finditer(content):
        args = {k: _coerce_xml_value(v) for k, v in _XML_PARAM_RE.findall(m.group(2))}
        tool_calls.append({
            "id": f"call_{len(tool_calls)}",
            "type": "function",
            "function": {"name": m.group(1), "arguments": json.dumps(args)},
        })

    if not tool_calls:
        return content, []

    # Whatever text isn't inside a <tool_call> block (a lead-in sentence,
    # or nothing at all) - real OpenAI-shaped responses carry empty/None
    # content alongside tool_calls, not the raw tag soup.
    remaining = _XML_CALL_RE.sub("", _TOOL_CALL_RE.sub("", content)).strip()
    return remaining, tool_calls

# One model kept loaded at a time, keyed by path - switching to a different
# .gguf unloads the previous one instead of accumulating several in RAM.
_loaded = {"path": None, "llm": None, "n_gpu_layers": None, "n_ctx": None, "gpu": None}

# Real bug found live 2026-09-13: some instruct models (e.g. Qwen2.5's own
# chat template supports an opt-in reasoning mode) emit reasoning text
# before their real answer, closed with a literal </think> tag. LM
# Studio's server separates this into its own "reasoning_content" field
# automatically; raw llama-cpp-python's create_chat_completion does NOT -
# it returns the reasoning concatenated into the same `content` string as
# the real answer. Left unstripped, this hits CASE twice: the user sees
# the model's raw internal monologue as if it were the answer, and -
# worse - case_agent.ask() saves that same bloated text into conversation
# history every turn, so a handful of turns can add tens of thousands of
# characters of reasoning text to history alone, exhausting even a large
# context window within a handful of messages (confirmed live: hit
# "Requested tokens (4107) exceed context window of 4096" by the fourth
# real turn).


def _strip_reasoning(text: str) -> tuple[str, str | None]:
    """Real observed shape, confirmed from an actual live transcript: the
    opening <think> tag is usually part of the chat template's OWN prompt
    prefix (consumed before generation even starts), so it's often absent
    from the completion text - only the closing </think> the model itself
    generates is actually present. Splitting on the LAST </think> handles
    this real shape (and, harmlessly, a rarer complete <think>...</think>
    pair too, arriving at the same result either way) - matching only on
    a paired regex would silently miss the case that matters. If nothing
    follows the last </think> (cut off exactly at the tag boundary before
    any real answer was generated - or no </think> at all, cut off mid-
    thought), fall back to the original text: showing an unfinished
    thought honestly beats showing an empty reply.

    Returns (visible_content, reasoning_or_none) - the reasoning used to
    just be thrown away here, but Sati asked to actually see it (2026-09-12),
    so it's captured and handed back for the caller to attach to the
    message instead of discarding it."""
    if "</think>" in text:
        before, after = text.rsplit("</think>", 1)
        after = after.strip()
        reasoning = before.replace("<think>", "").strip() or None
        if after:
            return after, reasoning
    return text, None


def gpu_backend_available() -> bool:
    """Whether the CURRENTLY INSTALLED llama-cpp-python build actually has
    a GPU backend compiled in (CUDA/Metal/OpenCL/Vulkan/etc) - a real,
    no-argument call into llama.cpp's own C API (confirmed live: returns
    False on this machine's plain CPU-only wheel), not guessed from
    hardware alone. Returns False (never raises) if llama-cpp-python
    isn't installed, or is an old version without this function - either
    way, "can't confirm GPU support" should behave like "no GPU support",
    not crash."""
    try:
        from llama_cpp import llama_supports_gpu_offload
        return bool(llama_supports_gpu_offload())
    except Exception:
        return False


def _gpu_layer_attempts(gpu) -> list:
    """Ordered n_gpu_layers values to try, most-aggressive first, always
    ending in 0 (CPU-only, guaranteed to work) as the last resort. -1 asks
    llama.cpp to offload every layer it can fit, automatically splitting
    the rest to CPU/RAM when the whole model doesn't fit in free VRAM.
    Only attempted at all if the hardware AND the installed build both
    support it - see gpu_backend_available()."""
    if not gpu or not gpu_backend_available():
        return [0]
    return [-1, 20, 10, 0]


# Ordered n_ctx values to try, most-generous first, always ending in the
# old safe 4096 as a guaranteed-small-enough last resort. 0 = the model's
# real trained context (e.g. 32768 for Qwen2.5-7B) - the correct, honest
# default (see _get_llm's own note on why 4096 was wrong as a blanket
# default), but a large KV cache needs real RAM/VRAM proportional to
# context size, and asking for the full native context unconditionally
# caused a genuine native crash - a low-level C++ exception (Windows
# error 0xe06d7363), not a clean, catchable Python one - on this exact
# machine while it was already low on free RAM (confirmed live: ~2.85GB
# free). Back off through smaller, real values rather than either
# extreme (always max = crash risk on tight-RAM machines; always the old
# fixed 4096 = the ORIGINAL bug, artificially exhausted within a handful
# of normal turns).
_N_CTX_ATTEMPTS = [0, 16384, 8192, 4096]


def get_load_info() -> dict:
    """What's actually loaded right now (or None fields if nothing is) -
    for the GUI/CLI to show the user real GPU-offload state instead of
    leaving "is it using my GPU?" unanswered after the fact."""
    return dict(_loaded)


def unload() -> bool:
    """Explicitly drop the currently loaded model, if any, freeing its
    GPU/RAM immediately - a real gap otherwise: switching the backend to
    "remote" in Settings does NOT free an already-loaded local model,
    nothing else clears this reference, so it silently stays resident
    until either a DIFFERENT local model is selected (which replaces it)
    or the whole app is closed. Calls Llama.close() explicitly - a real,
    documented method ("Explicitly free the model from memory"), not just
    dropping the Python reference and hoping __del__/GC gets to it in a
    useful timeframe. Returns True if something was actually unloaded,
    False if nothing was loaded to begin with (a no-op, not an error)."""
    llm = _loaded["llm"]
    if llm is None:
        return False
    try:
        llm.close()
    except Exception:
        pass  # best-effort - the reference is dropped below regardless, so GC still reclaims it eventually
    _loaded["path"] = None
    _loaded["llm"] = None
    _loaded["n_gpu_layers"] = None
    _loaded["n_ctx"] = None
    _loaded["gpu"] = None
    return True


def _get_llm(model_path: str):
    if _loaded["path"] != model_path:
        try:
            from llama_cpp import Llama
        except ImportError as e:
            raise RuntimeError(
                "Local backend selected but llama-cpp-python isn't installed. "
                "Install it with: pip install llama-cpp-python"
            ) from e

        gpu = case_hardware.get_gpu_info()
        gpu_attempts = _gpu_layer_attempts(gpu)
        llm = None
        last_error = None
        used_n_gpu_layers = None
        used_n_ctx = None
        # Context size OUTER, GPU layers INNER: try the most generous
        # context first (paired with every GPU-offload option) before
        # shrinking context at all - a real memory-pressure back-off, not
        # a guess at the "right" number for any given model/machine.
        # GPU options OUTER, context INNER (changed 2026-10-01): with context
        # outer, a model whose native context is huge (Qwen3.5-9B) failed
        # every GPU attempt at n_ctx=0, then succeeded CPU-only at n_ctx=0 -
        # silently ~50x slower (one answer took 30 minutes). Exhaust the
        # smaller contexts on the GPU before ever falling back to CPU.
        attempts = [(c, g) for g in gpu_attempts if g != 0 for c in _N_CTX_ATTEMPTS]
        attempts += [(c, 0) for c in _N_CTX_ATTEMPTS]
        for n_ctx, n_gpu_layers in attempts:
            try:
                llm = Llama(model_path=model_path, n_ctx=n_ctx, n_gpu_layers=n_gpu_layers, verbose=False)
                used_n_gpu_layers = n_gpu_layers
                used_n_ctx = n_ctx
                break
            except Exception as e:
                last_error = e
                continue
        if llm is None:
            raise RuntimeError(f"Failed to load local model {model_path}: {last_error}") from last_error

        _loaded["path"] = model_path
        _loaded["llm"] = llm
        _loaded["n_gpu_layers"] = used_n_gpu_layers
        _loaded["n_ctx"] = used_n_ctx
        _loaded["gpu"] = gpu
    return _loaded["llm"]


def _arguments_as_dicts(messages: list) -> list:
    """OpenAI wire format carries tool_calls[].function.arguments as a JSON
    STRING, but local chat templates iterate it as a mapping (Qwen3.5's does
    `arguments|items`, which crashed live 2026-10-01 with "Can only get item
    pairs from a mapping"). Return a copy with those strings parsed to dicts;
    Qwen2.5-style templates (`arguments|tojson`) accept a dict too."""
    fixed = []
    for m in messages:
        calls = m.get("tool_calls") if isinstance(m, dict) else None
        if calls:
            new_calls = []
            for c in calls:
                fn = dict(c.get("function", {}))
                if isinstance(fn.get("arguments"), str):
                    try:
                        parsed = json.loads(fn["arguments"] or "{}")
                        fn["arguments"] = parsed if isinstance(parsed, dict) else {}
                    except ValueError:
                        fn["arguments"] = {}
                new_calls.append({**c, "function": fn})
            m = {**m, "tool_calls": new_calls}
        fixed.append(m)
    return fixed


def chat_completion(messages: list, tools: list, model_path: str) -> dict:
    """Returns an OpenAI-chat-completion-shaped dict (choices[0].message...)
    so case_agent._call_model()'s caller doesn't need to know which backend
    answered - llama-cpp-python's create_chat_completion already returns
    this exact shape, including tool_calls when tools are supplied and the
    model's chat template supports them (not all GGUF models do - a model
    that ignores `tools` just answers in plain text, which case_agent.py's
    tool-calling loop already treats as a final answer with no tool_calls)."""
    llm = _get_llm(model_path)
    messages = _arguments_as_dicts(messages)
    kwargs = {"messages": messages, "temperature": 0.3, "max_tokens": 1200}
    if tools:
        kwargs["tools"] = tools
    try:
        result = llm.create_chat_completion(**kwargs)
    except Exception as e:
        raise RuntimeError(f"Local model inference failed: {e}") from e

    # Strip any leaked <think>...</think> reasoning block before this goes
    # back to case_agent.ask() - which both displays it to the user AND
    # saves it into permanent conversation history. See _strip_reasoning's
    # own docstring for why this matters beyond just tidiness. Then check
    # for a raw <tool_call>{...}</tool_call> block AFTER the reasoning
    # strip (a real completion can legitimately have both, reasoning
    # first) - see _parse_raw_tool_calls's own docstring for why this is
    # the actual fix for CASE's write-gating confirm_callback never
    # firing on the local backend.
    try:
        message = result["choices"][0]["message"]
        content = message.get("content")
        if content:
            content, reasoning = _strip_reasoning(content)
            content, parsed_tool_calls = _parse_raw_tool_calls(content)
            if parsed_tool_calls:
                message["tool_calls"] = parsed_tool_calls
            message["content"] = content or None
            if reasoning:
                message["reasoning"] = reasoning
    except (KeyError, IndexError, TypeError):
        pass  # unexpected response shape - return it as-is rather than crash on cleanup
    return result
