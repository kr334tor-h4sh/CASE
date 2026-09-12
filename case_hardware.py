"""
Real, on-device hardware capability checks for CASE's local-model backend -
so picking a model file that's obviously too heavy for THIS device's RAM
gives an upfront, honest warning instead of a silent hang/crash/thrash once
llama.cpp actually tries to load it.

Scope, stated plainly: this can only ever check the RAM of the device this
exact Python process is running ON right now - there is no way for the PC
to remotely learn the Mac mini's or a phone's hardware from here. It
naturally covers "PC, Mac mini, Android phone" because case_agent.py's
local backend is the same portable code on all three (Windows/macOS/Linux
including Termux's real Linux kernel) - whichever device actually runs
case_cli.py or case_gui_web.py locally gets checked, automatically. It does
NOT cover iPhone: there is no CASE client for iOS at all (no case_cli.py
equivalent runs there), so there is nothing for this module to check on
that platform - stated here rather than silently pretended.

Stdlib-only, platform-specific detection paths (Windows via ctypes,
macOS via sysctl/vm_stat, Linux/Android-via-Termux via /proc/meminfo) since
Python has no single portable "total system RAM" call.
"""

import ctypes
import os
import platform
import subprocess
from pathlib import Path

# Rough rule of thumb for llama.cpp CPU inference: total RAM needed is
# noticeably more than the raw file size (KV cache, context buffers,
# working memory) - 1.3x is a conservative-but-not-alarmist multiplier, not
# a precise model (context length/batch size/GPU offload all shift the real
# number) - this is meant to catch the "obviously won't fit" case, not to
# be an exact predictor.
RAM_OVERHEAD_MULTIPLIER = 1.3
# Never call a model a safe "ok" if it would need more than this fraction
# of total system RAM - the rest is needed for the OS, CASE itself, and
# everything else already running.
SAFE_RAM_FRACTION = 0.7
# Rough, honest heuristic, not a benchmark: above this file size, CPU-only
# inference is realistically "slow" (multi-minute replies), confirmed live
# 2026-09-13 with a real 27B model (3.8GB) taking an extremely long time
# with no GPU offload. Below it, CPU alone is generally fine.
CPU_FAST_THRESHOLD_BYTES = 3 * 1024**3
# Fraction of free VRAM a model's file size needs to fit under to call GPU
# offload "fast" (full offload) rather than "moderate" (partial) - leaves
# headroom for the KV cache and compute buffers, which are real memory on
# top of the raw weights.
GPU_FULL_OFFLOAD_FRACTION = 0.8


def get_total_ram_bytes():
    """Best-effort total physical RAM for the CURRENT device - None if it
    couldn't be determined (an unrecognized platform), which callers
    should treat as "unknown," not "zero capacity.\""""
    system = platform.system()
    try:
        if system == "Windows":
            return _windows_mem_status().ullTotalPhys
        if system == "Darwin":  # macOS - the Mac mini
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
            return int(out.stdout.strip()) if out.returncode == 0 else None
        if system == "Linux":  # includes Termux/Android - /proc/meminfo is real there too
            return _linux_meminfo_field("MemTotal:")
    except Exception:
        return None
    return None


def get_available_ram_bytes():
    """Best-effort CURRENTLY FREE physical RAM (not just total) - more
    honest than total RAM alone, since other running apps already claim
    some of it. Same platform coverage/caveats as get_total_ram_bytes."""
    system = platform.system()
    try:
        if system == "Windows":
            return _windows_mem_status().ullAvailPhys
        if system == "Darwin":
            page_size_out = subprocess.run(["sysctl", "-n", "hw.pagesize"], capture_output=True, text=True, timeout=5)
            page_size = int(page_size_out.stdout.strip()) if page_size_out.returncode == 0 else 4096
            vm_out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5)
            if vm_out.returncode != 0:
                return None
            free_pages = 0
            for line in vm_out.stdout.splitlines():
                if line.startswith("Pages free:") or line.startswith("Pages inactive:"):
                    free_pages += int(line.split(":")[1].strip().rstrip("."))
            return free_pages * page_size
        if system == "Linux":
            # MemAvailable was only added in kernel 3.14+ - confirmed live
            # that WSL1's kernel-translation layer does NOT expose it (real
            # /proc/meminfo on this machine's WSL1 Ubuntu has MemFree but no
            # MemAvailable line at all), so this can't assume it's always
            # there even though real Android/Termux kernels almost always
            # have it. MemFree is a real, always-present, more conservative
            # fallback (excludes reclaimable cache/buffers a real kernel
            # could free under pressure, so it UNDERESTIMATES what's truly
            # available rather than over-promising).
            available = _linux_meminfo_field("MemAvailable:")
            return available if available is not None else _linux_meminfo_field("MemFree:")
    except Exception:
        return None
    return None


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _windows_mem_status():
    stat = _MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        raise OSError("GlobalMemoryStatusEx failed")
    return stat


def _linux_meminfo_field(prefix: str):
    with open("/proc/meminfo", "r") as f:
        for line in f:
            if line.startswith(prefix):
                kb = int(line.split()[1])
                return kb * 1024
    return None


def _is_android() -> bool:
    """Real, standard markers - not a guess. TERMUX_VERSION is Termux's own
    env var, always set inside a Termux shell; ANDROID_ROOT/ANDROID_DATA
    are set by the OS itself on any real Android device (Termux included).
    platform.system() alone can't tell Android apart from any other Linux
    (Termux's Python reports "Linux", same as WSL1/generic Linux)."""
    return bool(os.environ.get("TERMUX_VERSION") or os.environ.get("ANDROID_ROOT") or os.environ.get("ANDROID_DATA"))


def _android_gpu_info():
    """Generic (not hardcoded to one phone/chip) Android GPU detection,
    real device-file checks, no root needed:
    - /dev/kgsl-3d0 is the actual kernel interface Qualcomm Adreno GPUs
      expose to userspace - its presence on ANY Adreno chip (not a
      specific model) is what lets Termux reach the real GPU driver
      directly, confirmed via real community reports (see
      ANDROID_SETUP.md's GPU section for sources). Reported backend is
      "opencl" since that's llama.cpp's official, Qualcomm-co-authored
      Adreno backend (docs/backend/OPENCL.md) - the best-documented path,
      not the only one (Vulkan also works on some Adreno chips, messier
      evidence).
    - Everything else (Mali/Exynos/MediaTek, or nothing detected): None.
      Real research (2026-09-13) found no working Termux GPU path for
      Mali - no equivalent direct kernel device, falls back to CPU
      software rendering - a genuine current hardware-vendor gap, not
      something checked here as a guess.
    - Adreno is unified memory, same situation as Apple Silicon below -
      no separate VRAM pool to query, so vram_total/vram_free mirror
      system RAM.
    IMPORTANT: this detects the HARDWARE only. Whether it actually
    accelerates anything additionally depends on the installed
    llama-cpp-python build actually being compiled with GPU support -
    that's a real, separate, non-default build step (see
    ANDROID_SETUP.md) this function has no way to check - that's what
    case_local_llm.gpu_backend_available() is for, checked separately at
    load time via llama.cpp's own real API, not guessed from hardware."""
    if not Path("/dev/kgsl-3d0").exists():
        return None
    name = "Qualcomm Adreno"
    try:
        chip = subprocess.run(["getprop", "ro.chipname"], capture_output=True, text=True, timeout=3)
        if chip.returncode == 0 and chip.stdout.strip():
            name = f"Qualcomm Adreno ({chip.stdout.strip()})"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return {
        "backend": "opencl",
        "name": name,
        "vram_total_bytes": get_total_ram_bytes(),
        "vram_free_bytes": get_available_ram_bytes(),
    }


def get_gpu_info():
    """Best-effort real GPU capability for local-model offload - None if no
    usable GPU acceleration was detected for this platform (a real "no,"
    not a failed guess): {"backend", "name", "vram_total_bytes",
    "vram_free_bytes"}.
    - NVIDIA (this PC): parsed from `nvidia-smi`, the standard tool any
      real NVIDIA driver install ships - a real, queryable VRAM number.
    - Apple Silicon (the Mac mini): Metal is available, but Apple's
      unified memory architecture means there's no separate VRAM pool to
      query - GPU and CPU share the same system RAM, so vram_total_bytes/
      vram_free_bytes mirror get_total_ram_bytes()/get_available_ram_bytes()
      rather than a real independent number.
    - Android/Termux: Qualcomm Adreno chips only (see _android_gpu_info) -
      generic across Adreno models, not hardcoded to one phone. Mali/
      Exynos/MediaTek: None, a real researched gap, not an oversight.
    - Everything else (AMD GPUs on desktop, generic Linux): None -
      genuinely not attempted. AMD/Vulkan support in llama.cpp exists but
      detecting and validating it is real, separate scope not covered here.
    """
    system = platform.system()
    try:
        if system == "Darwin" and platform.machine() in ("arm64", "aarch64"):
            return {
                "backend": "metal",
                "name": "Apple Silicon (unified memory)",
                "vram_total_bytes": get_total_ram_bytes(),
                "vram_free_bytes": get_available_ram_bytes(),
            }
        # NVIDIA check applies on Windows/Linux both - nvidia-smi is the
        # standard query tool either way if a real NVIDIA driver is installed.
        # Always absent on Android/Termux, so this cleanly falls through
        # via FileNotFoundError to the Android check below.
        nvidia_smi_kwargs = {"capture_output": True, "text": True, "timeout": 5}
        if system == "Windows":
            # Without this, spawning nvidia-smi.exe from CASE's windowless
            # pythonw.exe GUI briefly flashes a console window each call -
            # real, confirmed 2026-09-12 cause of the "window flickers"
            # reports (this function used to run twice per info-popover
            # click alone, looking like two windows flashing at once).
            nvidia_smi_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
            **nvidia_smi_kwargs,
        )
        if out.returncode == 0 and out.stdout.strip():
            # e.g. "NVIDIA GeForce RTX 3060 Ti, 8192, 7387" (MiB)
            first_line = out.stdout.strip().splitlines()[0]
            name, total_mib, free_mib = [x.strip() for x in first_line.split(",")]
            return {
                "backend": "cuda",
                "name": name,
                "vram_total_bytes": int(total_mib) * 1024 * 1024,
                "vram_free_bytes": int(free_mib) * 1024 * 1024,
            }
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
        pass
    if system == "Linux" and _is_android():
        return _android_gpu_info()
    return None


def assess_model_fit(model_path: str, gpu=None) -> dict:
    """Real verdict on whether THIS device can plausibly run the model at
    model_path:
    - "ok": should run within safe RAM headroom
    - "risky": would use most/all of what's actually free right now, or
      most of total RAM even if nothing else were running
    - "too_heavy": needs more than this device's TOTAL RAM, not just what's
      currently free - genuinely won't fit no matter what else is closed
    - "unknown": couldn't detect this platform's RAM - not a green light,
      just an honest "couldn't check," not a guess
    Always includes a plain-English `reason` explaining the numbers.

    `gpu`: pass an already-computed get_gpu_info() result to skip a
    redundant real subprocess spawn (nvidia-smi) - real, measured cost
    found live: listing N local .gguf files used to call this once PER
    FILE with no gpu passed, spawning N separate nvidia-smi processes for
    the exact same answer every time (confirmed ~0.45s wasted across 12
    files) - real, if modest, overhead during CASE's own startup. Leave
    None for a single standalone call, which still computes it fresh."""
    try:
        file_bytes = Path(model_path).stat().st_size
    except OSError as e:
        return {"verdict": "unknown", "reason": f"Couldn't read file size for '{model_path}': {e}"}

    estimated_ram_needed = int(file_bytes * RAM_OVERHEAD_MULTIPLIER)
    total_ram = get_total_ram_bytes()
    available_ram = get_available_ram_bytes()

    base = {
        "file_bytes": file_bytes,
        "estimated_ram_needed": estimated_ram_needed,
        "total_ram": total_ram,
        "available_ram": available_ram,
    }

    if total_ram is None:
        return {
            **base,
            "verdict": "unknown",
            "reason": f"Couldn't detect total RAM on this platform ({platform.system()}) - proceed carefully, no automatic check possible here.",
        }

    needed_gb = estimated_ram_needed / 1e9
    total_gb = total_ram / 1e9

    if estimated_ram_needed > total_ram:
        verdict = "too_heavy"
        reason = f"Needs an estimated ~{needed_gb:.1f}GB, but this device only has {total_gb:.1f}GB total RAM - won't fit no matter what else is closed."
    elif available_ram is not None and estimated_ram_needed > available_ram:
        verdict = "risky"
        reason = f"Needs an estimated ~{needed_gb:.1f}GB; only {available_ram/1e9:.1f}GB is free right now (of {total_gb:.1f}GB total) - close other apps first, or it may swap/hang."
    elif estimated_ram_needed > total_ram * SAFE_RAM_FRACTION:
        verdict = "risky"
        reason = f"Needs an estimated ~{needed_gb:.1f}GB, most of this device's {total_gb:.1f}GB total RAM - likely to work but leaves little headroom for anything else."
    else:
        verdict = "ok"
        reason = f"Estimated ~{needed_gb:.1f}GB needed, comfortably within this device's {total_gb:.1f}GB RAM."

    if gpu is None:
        gpu = get_gpu_info()
    speed_estimate, speed_reason = _estimate_speed(file_bytes, gpu)

    return {**base, "verdict": verdict, "reason": reason, "gpu": gpu, "speed_estimate": speed_estimate, "speed_reason": speed_reason}


def _estimate_speed(file_bytes: int, gpu) -> tuple:
    """Rough, honest "fast"/"moderate"/"slow" expectation - separate from
    the RAM verdict above, because a model can comfortably FIT in RAM and
    still be painfully slow to actually run (confirmed live: a 27B model
    at CPU-only inference "fit" fine and still took minutes per reply).
    Not a benchmark - a real, stated heuristic based on file size and
    whether a real GPU with enough free VRAM was detected."""
    if gpu and gpu.get("vram_free_bytes"):
        if file_bytes <= gpu["vram_free_bytes"] * GPU_FULL_OFFLOAD_FRACTION:
            return "fast", f"Should fit fully on the detected {gpu['name']} ({gpu['backend']}) - offload the whole model to GPU."
        return "moderate", f"Larger than what fits fully on the detected {gpu['name']}'s free VRAM - partial GPU offload still helps, but won't be as fast as a full fit."
    if file_bytes <= CPU_FAST_THRESHOLD_BYTES:
        return "fast", "Small enough that CPU-only inference should feel responsive."
    return "slow", "No GPU acceleration detected/usable, and this file is large enough that CPU-only inference will likely take minutes per reply, not seconds."
