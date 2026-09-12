"""
Voice engine for CASE - mic capture, VAD, speech-to-text, and text-to-speech,
adapted from the standalone TARS Voice project (../TARS Voice/tars_control.py)
but wired directly into CASE's own in-process call path instead of that
project's Windows-only "find the Claude window and paste into it" mechanism
(pygetwindow + pyautogui). CASE doesn't need any of that: it already has a
real Python function (case_agent.ask()) to call directly, in the same
process - no GUI automation, no window-matching fragility, and it's the same
code on Windows/Linux/Android since nothing here touches window handles.

Architecture: this module only produces TRANSCRIPTS (via a thread-safe queue)
and SPEAKS text handed to it - it never calls case_agent.ask() itself. The
caller (case_gui_web.py) drains transcripts, runs them through the same
turn-processing path as a typed message, and hands the reply back here to
speak. Keeps this module a reusable, dependency-isolated engine rather than
something that has to know about sessions/history/confirmation dialogs.

All of faster-whisper/torch/sounddevice/soundfile/f5_tts are OPTIONAL
dependencies, same as llama-cpp-python for case_local_llm.py - only imported
the first time voice is actually started, so a text-only CASE install never
needs any of this.
"""

import os
import queue
import threading
import time
import traceback
from pathlib import Path

import case_agent

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 512     # Silero VAD's minimum frame size at 16kHz
SILENCE_CHUNKS = 60     # ~1.9s of silence = end of utterance (same tuning as TARS Voice v2)
MIN_SPEECH_CHUNKS = 6   # discard sub-~0.2s blips (coughs, mic bumps)
VAD_THRESHOLD = 0.45
MAX_TTS_WORDS = 180     # F5-TTS degrades past ~200 words per call

# Fallback voice reference: reuses TARS Voice's own reference clip until a
# CASE-specific one is acquired via the same acquire-voice pipeline - a real,
# working voice today rather than blocking this feature on finding genuine
# Interstellar CASE-robot audio first. Settings > Voice makes this visible
# and overridable, not silently baked in.
_DEFAULT_REF_CLIP = str(Path(__file__).parent.parent / "TARS Voice" / "samples" / "tars_reference_demucs.wav")
_DEFAULT_REF_TEXT = (
    "Confirmed. Additional customization. 60% confirmed. "
    "Knock knock. Goodbye Doctor Brand. See you on the other side, Coop."
)

_state = {
    "listening": False,
    "speaking_enabled": False,
    "models_loading": False,
    "models_ready": False,
    "load_error": None,
    "currently_speaking": False,
    "mic_device": None,
}

_transcript_queue = queue.Queue()   # text the mic heard, drained by the caller
_speak_queue = queue.Queue()        # text to synthesize + play
_whisper = None
_vad_model = None
_tts = None
_stream = None
_audio_queue = queue.Queue(maxsize=200)
_threads_started = False


def voice_deps_available() -> bool:
    """Real check, not a guess: can the optional voice stack even be
    imported on this machine right now. Settings > Voice uses this to show
    an honest 'not installed' state instead of a button that silently fails."""
    try:
        import faster_whisper  # noqa: F401
        import torch  # noqa: F401
        import sounddevice  # noqa: F401
        import soundfile  # noqa: F401
        from f5_tts.api import F5TTS  # noqa: F401
        return True
    except ImportError:
        return False


def get_voice_config() -> dict:
    config = case_agent._load_config()
    voice = config.get("voice") or {}
    return {
        "mic_device": voice.get("mic_device"),
        "ref_clip": voice.get("ref_clip") or _DEFAULT_REF_CLIP,
        "ref_text": voice.get("ref_text") or _DEFAULT_REF_TEXT,
        "using_default_voice": not bool(voice.get("ref_clip")),
    }


def set_voice_config(mic_device=None, ref_clip: str = "", ref_text: str = "") -> None:
    config = case_agent._load_config()
    config["voice"] = {
        "mic_device": mic_device,
        "ref_clip": ref_clip.strip(),
        "ref_text": ref_text.strip(),
    }
    case_agent._save_config(config)


def list_input_devices() -> list:
    """[{"index", "label"}] for every mic-capable device - empty list (not
    an error) if sounddevice/PortAudio isn't installed or no input device
    exists, so the Settings UI can show 'no microphone found' plainly."""
    try:
        import sounddevice as sd
        devices = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                devices.append({"index": i, "label": f"[{i}] {d['name'][:50]}"})
        return devices
    except Exception:
        return []


def _log_error(context: str, exc: Exception) -> None:
    _state["load_error"] = f"{context}: {type(exc).__name__}: {exc}"
    traceback.print_exc()


def _load_models() -> None:
    global _whisper, _vad_model, _tts
    _state["models_loading"] = True
    try:
        import torch
        from faster_whisper import WhisperModel
        device = "cuda" if torch.cuda.is_available() else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        _whisper = WhisperModel("small.en", device=device, compute_type=compute_type)

        _vad_model, _ = torch.hub.load("snakers4/silero-vad", "silero_vad", force_reload=False, onnx=False)

        from f5_tts.api import F5TTS
        _tts = F5TTS()
        cfg = get_voice_config()
        _tts.infer(ref_file=cfg["ref_clip"], ref_text=cfg["ref_text"], gen_text="Warming up.", remove_silence=False)

        _state["models_ready"] = True
    except Exception as e:
        _log_error("load_models", e)
    finally:
        _state["models_loading"] = False


def _audio_callback(indata, frames, time_info, status):
    if not _state["listening"] or _state["currently_speaking"]:
        return
    try:
        import numpy as np
        chunk = indata[:, 0].astype(np.float32).copy()
        _audio_queue.put_nowait(chunk)
    except queue.Full:
        pass
    except Exception:
        pass


def _vad_worker():
    import numpy as np
    import torch
    audio_buffer = []
    silence_count = 0
    speech_chunks = 0
    recording = False

    while True:
        try:
            chunk = _audio_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        if not _state["models_ready"] or not _state["listening"]:
            continue
        try:
            prob = _vad_model(torch.from_numpy(chunk), SAMPLE_RATE).item()
            is_speech = prob > VAD_THRESHOLD
        except Exception as e:
            _log_error("vad", e)
            continue

        if is_speech:
            if not recording:
                recording = True
                speech_chunks = 0
            silence_count = 0
            speech_chunks += 1
            audio_buffer.append(chunk)
        elif recording:
            silence_count += 1
            audio_buffer.append(chunk)
            if silence_count >= SILENCE_CHUNKS:
                recording = False
                if speech_chunks >= MIN_SPEECH_CHUNKS:
                    audio = np.concatenate(audio_buffer)
                    threading.Thread(target=_transcribe, args=(audio,), daemon=True).start()
                audio_buffer.clear()
                silence_count = 0
                speech_chunks = 0


def _transcribe(audio) -> None:
    try:
        segments, _ = _whisper.transcribe(audio, beam_size=1, language="en")
        text = " ".join(s.text.strip() for s in segments).strip()
        if text:
            _transcript_queue.put(text)
    except Exception as e:
        _log_error("transcribe", e)


def _speaker_loop() -> None:
    while True:
        text = _speak_queue.get()
        if not _state["models_ready"] or not _state["speaking_enabled"]:
            continue
        _state["currently_speaking"] = True
        try:
            import numpy as np
            import sounddevice as sd
            import soundfile as sf
            words = text.split()
            if len(words) > MAX_TTS_WORDS:
                text = " ".join(words[:MAX_TTS_WORDS]) + "..."
            if text and text[-1] not in ".!?,":
                text += "."
            cfg = get_voice_config()
            wav, sr, _ = _tts.infer(ref_file=cfg["ref_clip"], ref_text=cfg["ref_text"], gen_text=text, remove_silence=False)
            silence = np.zeros(int(sr * 0.5), dtype=wav.dtype)
            wav = np.concatenate([wav, silence])
            out_dir = Path(__file__).parent / "voice_output"
            out_dir.mkdir(exist_ok=True)
            out_wav = out_dir / "case_speak.wav"
            sf.write(str(out_wav), wav, sr)
            data, samplerate = sf.read(str(out_wav))
            sd.play(data, samplerate)
            sd.wait()
            time.sleep(0.3)
        except Exception as e:
            _log_error("speak", e)
        finally:
            _state["currently_speaking"] = False


def _ensure_threads_started() -> None:
    global _threads_started
    if _threads_started:
        return
    _threads_started = True
    threading.Thread(target=_load_models, daemon=True).start()
    threading.Thread(target=_vad_worker, daemon=True).start()
    threading.Thread(target=_speaker_loop, daemon=True).start()


def start_listening(mic_device=None) -> str:
    """Begin mic capture. Requires the models to already be ready or still
    loading (kicked off lazily on first call, same pattern as
    case_local_llm's lazy model load) - returns a plain status string rather
    than raising, since the caller (Settings > Voice) just needs to show it."""
    global _stream
    if not voice_deps_available():
        return "Voice dependencies aren't installed. See CASE's voice setup notes."
    _ensure_threads_started()
    if _state["load_error"]:
        return f"Voice failed to load: {_state['load_error']}"

    try:
        import sounddevice as sd
    except ImportError:
        return "sounddevice isn't installed."

    if _stream is not None:
        stop_listening()

    device = mic_device if mic_device is not None else _state["mic_device"]
    try:
        _stream = sd.InputStream(
            device=device, samplerate=SAMPLE_RATE, channels=1,
            blocksize=CHUNK_SAMPLES, dtype="float32", callback=_audio_callback,
        )
        _stream.start()
    except Exception as e:
        _stream = None
        return f"Couldn't open microphone: {e}"

    _state["listening"] = True
    _state["mic_device"] = device
    return "Listening." if _state["models_ready"] else "Listening (models still loading in the background)."


def stop_listening() -> str:
    global _stream
    _state["listening"] = False
    if _stream is not None:
        try:
            _stream.stop()
            _stream.close()
        except Exception:
            pass
        _stream = None
    return "Stopped listening."


def set_speaking_enabled(enabled: bool) -> None:
    _state["speaking_enabled"] = bool(enabled)
    if not enabled:
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass


def speak(text: str) -> None:
    """Queue text to be spoken - non-blocking, returns immediately. A no-op
    if speaking isn't enabled or models aren't ready (checked inside the
    speaker loop, not here, so toggling speech on mid-queue still works)."""
    if text and text.strip():
        _ensure_threads_started()
        _speak_queue.put(text.strip())


def drain_transcripts() -> list:
    """Pop every transcript heard since the last call - the caller
    (case_gui_web.py) turns each into a real case_agent.ask() call."""
    out = []
    while not _transcript_queue.empty():
        try:
            out.append(_transcript_queue.get_nowait())
        except queue.Empty:
            break
    return out


def get_status() -> dict:
    return {
        "listening": _state["listening"],
        "speaking_enabled": _state["speaking_enabled"],
        "currently_speaking": _state["currently_speaking"],
        "models_loading": _state["models_loading"],
        "models_ready": _state["models_ready"],
        "load_error": _state["load_error"],
        "deps_available": voice_deps_available(),
    }
