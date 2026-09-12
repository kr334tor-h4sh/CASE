# CASE on Android (Termux) — setup guide

Status as of 2026-09-13: the underlying mechanism is **proven** via a real
network round-trip from a genuinely separate Linux environment (WSL1
Ubuntu) to this PC over the actual LAN — 20/20 checks, including a full
chat round-trip with zero local project files. It has **not** been tested
on a real or emulated Android device — Termux/Android-specific behavior
(background execution, battery kill, permissions) is a real remaining
unknown, not something WSL1 can validate. Read the Limitations section
before assuming this "just works" on a phone.

Throughout this doc, "the PC" and "the Mac mini" refer to this author's own
two-machine setup (a Windows PC running CASE, a Mac mini hosting a remote
LM Studio/Bionic server on the LAN) - purely as concrete stand-ins for
whatever your own "device running CASE" and "device hosting a remote
model" actually are. Nothing here requires a Mac specifically; any machine
running an OpenAI-compatible server (LM Studio, Bionic, or similar) works
the same way, and the on-device local-inference path (no remote server at
all) works too - see "GPU acceleration on Android" and "Hardware
capability check" below.

## What ships to the phone

Copy only these files into a folder on the phone (via Termux's own file
access, a cloud sync, USB, whatever's convenient):
- `case_agent.py`, `case_tools.py`, `case_cli.py`, `case_skills.py`,
  `case_web_search.py`, `case_browser.py` (safe to include even though its
  actual browse/open-window features need pywebview and won't work on
  Android — `case_tools.py` imports it but never crashes if pywebview
  itself is never invoked)
- the whole `skills/` folder (a few KB of text — simpler to just copy than
  to add network complexity for something this small)

Do **NOT** copy: `case_write_tools.py`, `case_gui_web.py`/`.html`,
`case_mcp_server.py`, `case_sessions.py`, `case_local_llm.py`,
`case_file_server.py` (that one stays on the PC — see below), or
`case_config.json` (phone has its own env vars instead, see below).

## On the PC (do this first, every time you want phone access)

1. Run the file server: `python case_file_server.py` — leave it running.
   It serves `list_directory`/`read_file`/`search_files`/`memory_reflect`
   over plain HTTP on port 8765, bound to all interfaces so it's LAN-
   reachable, not just localhost.
2. First run may trigger a Windows Defender Firewall prompt for Python —
   allow it on your private/home network.
3. Find this PC's LAN IP (`ipconfig`, the Wi-Fi or Ethernet adapter's
   IPv4 address) — the phone needs this, it'll change if your router
   reassigns it, a static DHCP reservation avoids re-discovering it.

## On the phone (Termux)

1. Install **Termux from F-Droid, not the Play Store** — the Play Store
   build is deprecated (2020) and broken by newer Android's exec()
   restrictions; only the F-Droid build still works for this.
2. `pkg install python`
3. Copy the files listed above into Termux's home or a subfolder.
4. Set these before running (put them in `~/.bashrc` so they persist):
   ```bash
   export CASE_FILE_SERVER_URL="http://<PC's LAN IP>:8765"
   ```
   `CASE_PROJECT_ROOT`/`CASE_MEMORY_ROOT` don't need to be set — they're
   only used when `CASE_FILE_SERVER_URL` is empty, and constructing an
   unused `Path()` from their Windows-style default never touches the
   filesystem, so it's harmless either way.
5. Run it: `python case_cli.py`

That's the whole client — a real chat loop against the Mac mini's model
(or wherever `case_config.json`-equivalent remote endpoint points, default
unchanged) with real file/memory tool access proxied back to the PC, and
zero project data ever duplicated onto the phone.

**File attach from the phone**: type `/attach <path>` (any file the phone
can actually read - Termux's own storage, or wherever you've set up
storage access) before your next message - it's included the same way the
GUI's 📎 button does it. Same underlying function on both clients
(`case_agent.compose_message_with_attachment`), verified working
identically on real Linux paths via WSL1.

**Model info from the phone**: type `/model` to see the same info the
GUI's header ℹ️ popover shows - backend, model, and for a local model its
real context size/GPU offload/GPU name. Same underlying function on both
clients (`case_agent.get_model_info_summary()`), deliberately not run at
startup (it probes the GPU, a real subprocess spawn) - only on request.

**Workspace/memory folder is a shared setting, not per-client** - Settings
> General on the GUI (or editing `case_config.json` directly) sets
`project_root`/`memory_root`, and every client that imports `case_tools.py`
- the GUI, `case_cli.py`, and `case_file_server.py` - reads the SAME
config. Practically: changing the workspace folder on the PC changes what
an Android client sees through the file server too, automatically - no
separate phone-side setting exists or is needed.

## What's proven vs. not

**Proven** (via WSL1 Ubuntu → real PC, actual LAN IP, 2026-09-13):
- `case_tools.py`'s remote-proxy mode (`CASE_FILE_SERVER_URL`) - all four
  tools (list/read/search/memory_reflect) return real, correct data with
  literally zero local project files present.
- A full `case_agent.ask()` round trip - real model inference (Mac mini)
  + a real tool call proxied over the network - works end to end.
- `case_skills.py` works identically on Linux paths (skills are just
  local files, no network involved).
- `case_write_tools.py`'s backup-before-overwrite mechanism works
  correctly on Linux paths too (not that Android CASE uses it -
  `case_cli.py` is deliberately read-only, see below).

**Not proven, real open questions**:
- Real Android/Termux behavior specifically: background execution surviving
  Android's battery/task-kill policies (a real, unresolved concern per
  termux-app's own open issues, not CASE-specific), storage permission
  prompts, whether `python case_cli.py` runs the same way in Termux as in
  WSL1's genuine glibc Linux (Termux's Bionic libc is a different C
  library - stdlib-only code like this should be fine, but "should be" is
  not "verified").
- The PC's own LM Studio (as opposed to the Mac mini) is only reachable
  from the phone if you enable LAN binding in its server settings
  (Developer tab) - confirmed today it defaults to localhost-only, unlike
  the Mac mini's instance which is already LAN-reachable. Only matters if
  you want the phone's subagent/primary pointed at this PC specifically.
- On-device local inference (no network needed at all) is architecturally
  already supported - `case_agent.py`'s local backend (`case_local_llm.py`,
  llama.cpp) is the same code on any platform, and llama.cpp genuinely has
  mainstream ARM support (real, shipped Android apps - ChatterUI, PocketPal
  AI, Layla - already run normal GGUF models this way). The one untested
  link: whether `pip install llama-cpp-python` actually builds cleanly in
  real Termux specifically (only verified on Windows x64 so far) - likely
  to work given the precedent, but not confirmed firsthand.

  **Bonsai specifically - answered 2026-09-13, real no, not just untested.**
  Checked two independent angles: (1) PrismML's own site
  (prismml.com/news/bonsai-27b) states its low-bit kernels run "natively...
  via MLX and on NVIDIA GPUs via CUDA" - Apple Silicon and Nvidia GPU only,
  zero mention of Android/ARM CPU despite the "runs on a phone" headline
  (that phone is iPhone, via MLX). (2) The closest real candidate for
  ternary/1-bit models on ARM CPU, Microsoft's `bitnet.cpp`
  (github.com/microsoft/BitNet) - confirmed real ARM CPU support (1.37x-
  5.07x speedup) - only supports models specifically packaged for its own
  quantization types (`i2_s`, `tl1`): BitNet b1.58, a 1.58-bit Llama3-8B,
  and the Falcon3/Falcon-E family. No mention of Bonsai or PrismML
  anywhere in its docs, and it doesn't claim Android/Termux support either
  - it's a different, incompatible ternary format, not a generic runtime
  Bonsai's GGUF would just drop into. **Verdict: don't plan around Bonsai
  for the Android build.** For a real on-device model, pick a normal
  small model in standard llama.cpp quantization (Q4_K_M etc.) - e.g. a
  1-3B Gemma/Qwen/Phi variant - not an exotic low-bit format with no
  confirmed ARM runtime.

## Local backend fixes (2026-09-13) - directly unblocks Android tool-calling

Three real bugs were found and fixed in `case_local_llm.py` while testing
CASE's local backend on the PC - all pure Python, no OS-specific code, so
they apply identically here with zero porting work (verified directly on
real WSL1 Linux, not just assumed):

1. **Context window is no longer hardcoded to 4096.** It now tries the
   model's real native context first (`n_ctx=0`, llama-cpp-python's own
   "use the model's trained context" sentinel), backing off through
   `16384`/`8192`/`4096` if the full size doesn't fit available memory.
   Matters more on Android than the PC - RAM is scarcer, so a model that
   loads fine at a smaller context (rather than crashing on the full
   native one, or being artificially capped at a low fixed value) is a
   real, direct benefit here.
2. **Leaked `<think>...</think>` reasoning is stripped** before it reaches
   the user or gets saved into conversation history. Real observed shape:
   the opening `<think>` tag is usually consumed by the chat template's
   own prompt prefix, so only the closing `</think>` is actually present
   in the completion text - the parser splits on the LAST `</think>`, not
   a paired-tag regex, to handle this correctly. Matters a lot on
   constrained hardware: unstripped reasoning bloats every turn's history,
   accelerating context exhaustion exactly where memory is already tight.
3. **Tool-calling now actually works locally - this is the big one for
   Android specifically.** Raw llama-cpp-python has no native parser for
   Qwen2.5's (and other Hermes-style models') tagged tool-call format -
   `<tool_call>\n{"name": ..., "arguments": {...}}\n</tool_call>` - so a
   write/delete confirmation gate never fired at all before this fix,
   silently. On the PC this was a real gap because LM Studio's server
   (which DOES parse this) was available as an alternative; **on Android
   there is no LM Studio to fall back to at all** - this raw-parser gap
   was the actual, structural reason local tool-calling looked unusable
   there in earlier research, not just small-model unreliability. A real
   `_parse_raw_tool_calls()` function now handles this format directly in
   `case_local_llm.py` itself - verified live (confirm_callback fires,
   file actually gets written) on the PC, and the same parsing logic
   confirmed correct on genuine Linux via WSL1. This changes the earlier
   "tool-calling gets unreliable this small" caveat from a hard
   architectural blocker to a genuine model-capability question again -
   Qwen2.5-family models specifically should now have a real, working
   tool-calling path on Android too, not just on the PC/Mac mini.

Also new: an explicit `unload()` a model can call to free GPU/RAM
immediately (`case_agent.unload_local_model()`) - real, live-verified to
free ~6.5GB RAM/~7GB VRAM on the PC - directly useful on a phone where
freeing memory between uses matters more than on a desktop.

**Verified CASE's local backend has zero dependency on LM Studio/any
server** - confirmed live by stopping the PC's own LM Studio server
entirely (`lms server stop`, confirmed unreachable via curl) and running
a real CASE conversation + a real gated write through the local backend
anyway - both worked identically. This is exactly the Android situation
(no LM Studio exists there at all) proven out on the PC first.

## GPU acceleration on Android - automatic detection, real but narrow (2026-09-13)

`case_hardware.get_gpu_info()` now detects Qualcomm Adreno GPUs on Android
generically (not hardcoded to one phone/chip): it checks for
`/dev/kgsl-3d0`, the real kernel device node any Adreno chip exposes
directly to userspace - no root, no compiled app needed, this is what
lets plain Termux reach the actual GPU driver at all. If present, it
reports a real (if approximate - phones are unified memory, same as the
Mac mini's Apple Silicon) GPU capability; if absent (Mali/Exynos/
MediaTek chips, or a non-Android Linux box), it correctly reports no GPU,
same as before.

**Detecting the hardware is only half the picture.** `case_local_llm.
gpu_backend_available()` separately checks whether the INSTALLED
llama-cpp-python build was actually compiled with a GPU backend, via
llama.cpp's own real API (`llama_supports_gpu_offload()`) - confirmed
live to correctly return `False` on a plain CPU-only build. Both have to
be true before CASE attempts GPU offload at all; if either is false, it
loads CPU-only immediately, no wasted attempt, no ambiguity. Settings >
Local Models shows which case you're in: no GPU, GPU-but-not-built-in,
or GPU-and-ready.

**Real research verdict (2026-09-13) on what it takes to get the second
half true on Android**: llama.cpp's OpenCL backend is the official,
Qualcomm-co-authored path for Adreno (`docs/backend/OPENCL.md`,
Qualcomm's own dev blog) - but there is NO official Termux build recipe.
One real community report (GitHub Discussion #23736) got it working on a
Snapdragon 8 Elite (Adreno 830) via a manual ICD wire-up:
```bash
# Experimental - not an official/supported CASE build step. Requires a
# device with the /dev/kgsl-3d0 node CASE already detects.
export LD_LIBRARY_PATH=/vendor/lib64
export CMAKE_ARGS="-DGGML_OPENCL=ON -DGGML_OPENCL_USE_ADRENO_KERNELS=ON"
pip install llama-cpp-python --force-reinstall --no-cache-dir
```
plus a manual OpenCL ICD entry pointing at
`/vendor/lib64/libOpenCL_adreno.so` (exact steps vary by device/ROM -
follow the discussion thread, not a fixed recipe here). Real measured
result from that one report: 24x prefill / 2.7x generation speedup on a
1.5B model - but on a 7B model, generation was 17% SLOWER than CPU
(GPU/CPU sync overhead outweighed the offload benefit). **Treat this as
experimental and model-size-dependent, not a guaranteed win** - if you
go through this, CASE will notice and use it automatically (that's the
whole point of the detection above), but the build step itself is a
real, separate, per-device, currently-unofficial thing you'd be doing,
not something CASE can do for you. Vulkan is a lower-priority
alternative with messier evidence (works well on some Adreno chips,
crashes/gibberish on others - see case_agent_project.md's full research
notes for sources). Mali/Exynos/MediaTek: no working Termux GPU path
exists currently, for any backend - CPU-only is correct there, not a gap
in CASE's own detection.

## Hardware capability check (RAM) - automatic, per-device

`case_hardware.py` checks THIS device's actual RAM (Windows via ctypes,
macOS/Mac mini via `sysctl`/`vm_stat`, Linux/Android-via-Termux via
`/proc/meminfo`) before recommending a local model, and flags each
detected `.gguf` as ok / risky / too_heavy with a plain-English reason
(estimated RAM need vs. what's actually free right now vs. total). This is
automatic and per-device by design - there's no way for the PC to remotely
know a phone's or the Mac mini's hardware, so whichever device actually
runs `case_cli.py`/`case_gui_web.py` locally gets checked using ITS OWN
real numbers. Verified on a real 17GB Windows PC and, separately, via
WSL1's real `/proc/meminfo` (with one real fix along the way: WSL1's
kernel-translation layer doesn't expose the newer `MemAvailable` field at
all - confirmed by direct inspection - so the Linux path now falls back to
the always-present `MemFree` field, more conservative but real; genuine
Android/Termux kernels are expected to have `MemAvailable` normally, but
this fallback means it degrades gracefully either way). Not implemented
for iPhone - there's no CASE client for iOS, nothing to check there.

## Why read-only on the phone, same as the PC's own case_cli.py

`case_cli.py` never sets `allow_write=True` - no write/delete tools are
ever offered, on the PC or the phone, by design (same reasoning as the
PC's terminal client: no confirmation mechanism exists there to gate a
write safely). If write access from a phone is ever wanted, it needs its
own confirmation mechanism first (a push notification you approve, e.g.) -
not something to bolt on silently.
