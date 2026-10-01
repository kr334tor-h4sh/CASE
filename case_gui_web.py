"""
CASE - the standalone GUI client, styled to resemble Claude's own chat
interface. Uses pywebview to render real HTML/CSS/JS (case_gui_web.html) via
the OS's built-in WebView2/Edge engine - same lightweight-by-design approach
as case_browser.py, no separate browser binary bundled. Replaced the earlier
tkinter case_gui.py outright (retired 2026-09-12) once this was verified to
do everything it did, plus write access.

This client offers CASE write_file/delete_file access - gated by a REAL
native OS confirmation dialog (pywebview's Window.create_confirmation_dialog,
WinForms.MessageBox on Windows) before any write/delete actually executes.
This is the standalone equivalent of Bionic's own tool-confirmation prompt -
it's what makes write access safe here without needing Bionic's UI at all.
The "Allow file changes" checkbox in the UI controls whether write tools are
even offered to the model for a given message; unchecked, this is a plain
read-only session.

Verified working end-to-end 2026-09-12: write/deny/delete gating logic
confirmed via 10 automated checks against the real model (approve and deny
both do the right thing, path-traversal blocked, missing confirm_callback
fails closed); window opens, dark theme renders, welcome message fires; the
Browser button and the real native confirmation dialog both confirmed
working live. One real bug found and fixed along the way: the `Api.window`
attribute (no leading underscore) was being walked by pywebview's own
get_functions() when building the JS bridge, recursing into the live Window
object -> window.native (WinForms) -> AccessibilityObject.Bounds, a
self-referential .NET property chain - froze the UI thread with the window
open but "Not Responding". Fixed by renaming it to `_window`; pywebview
skips any leading-underscore attribute on a js_api object, so it's never
walked. Worth remembering for any FUTURE attribute added to this class:
anything not meant to be introspected this way needs the same underscore.

Usage:
    python case_gui_web.py
"""

import ctypes
import msvcrt
from pathlib import Path

import webview

import case_agent
import case_browser
import case_browser_panel
import case_sessions
import case_skills
import case_tools
import case_voice

HTML_FILE = Path(__file__).parent / "case_gui_web.html"
LOCK_FILE = Path(__file__).parent / "case_gui.lock"
WINDOW_TITLE = "CASE"

_lock_handle = None  # kept open for the process's lifetime - Windows releases it automatically on exit, even a crash


def _acquire_single_instance_lock() -> bool:
    """True if this is the only running case_gui_web.py instance. A second
    launch while one is already up (or still shutting down) used to race
    WebView2's exclusive lock on its own user-data-folder - that hang had no
    visible window and no error (pythonw.exe shows nothing), so double-
    clicking the desktop shortcut while impatient looked like it silently
    did nothing. This makes a second launch attempt a no-op instead."""
    global _lock_handle
    try:
        _lock_handle = open(LOCK_FILE, "a+")
        msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        if _lock_handle:
            _lock_handle.close()
            _lock_handle = None
        return False


def _focus_existing_window():
    hwnd = ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE - un-minimizes if needed
        ctypes.windll.user32.SetForegroundWindow(hwnd)


class Api:
    """Methods exposed to the JS side via window.pywebview.api.*"""

    def __init__(self):
        self.model = case_agent.get_selected_model()
        # Leading underscore is load-bearing: pywebview's get_functions() walks every
        # non-underscore attribute of a js_api object to build the JS bridge, and would
        # otherwise recurse into the real Window -> window.native (WinForms) ->
        # AccessibilityObject.Bounds, a self-referential .NET property chain that hangs
        # the UI thread (confirmed live 2026-09-12 - window opened but stopped responding).
        self._window = None  # set after the window is created, needed for the confirmation dialog
        self.current_session_id = None
        self.history = []
        retention_days = case_agent.get_sessions_retention_days()
        if retention_days:
            case_sessions.delete_sessions_older_than(retention_days)
        self._start_on_fresh_or_existing_empty_chat()

    # --- Sessions / Projects ---
    def _start_on_fresh_or_existing_empty_chat(self):
        """Startup should always FEEL like a new conversation (Sati's
        preference, 2026-09-13) - but a real bug in the first version of
        that fix: unconditionally creating a brand new session on every
        single launch piled up empty, never-used "New chat" entries in
        the sidebar every time CASE was opened and closed without typing
        anything (confirmed by Sati: "even if the old one stayed the same
        like nothing was written in it"). Fix: only create a genuinely
        NEW session if the most recent one actually has something in it;
        if the most recent session is still untouched (just the system
        prompt, no real exchange), resume THAT one instead - it already
        IS an empty new chat, no need for a second one."""
        existing = case_sessions.list_all_sessions()
        if existing:
            most_recent = case_sessions.load_session(existing[0]["id"])
            if most_recent is not None and len(most_recent["history"]) <= 1:
                self.current_session_id = most_recent["id"]
                self.history = most_recent["history"]
                return
        self._create_new_session()

    def _activate_most_recent_or_new(self):
        existing = case_sessions.list_all_sessions()
        if existing:
            self._activate_session(existing[0]["id"])
        else:
            self._create_new_session()

    def _activate_session(self, session_id: str):
        record = case_sessions.load_session(session_id)
        if record is None:
            self._create_new_session()
            return
        self.current_session_id = session_id
        self.history = record["history"]

    def _create_new_session(self, project_id=None):
        record = case_sessions.create_session(case_agent.new_history(), project_id=project_id)
        self.current_session_id = record["id"]
        self.history = record["history"]
        return record

    def get_current_session(self) -> dict:
        """Called on page load / after switching chats - JS re-renders the
        visible chat log from this instead of assuming an empty window."""
        return case_sessions.load_session(self.current_session_id)

    def list_chats(self) -> list:
        return case_sessions.list_all_sessions()

    def list_projects(self) -> list:
        return case_sessions.list_projects()

    def create_project(self, name: str) -> dict:
        return case_sessions.create_project(name)

    def delete_project(self, project_id: str) -> None:
        case_sessions.delete_project(project_id)

    def new_chat(self, project_id=None) -> dict:
        return self._create_new_session(project_id=project_id or None)

    def switch_chat(self, session_id: str) -> dict:
        self._activate_session(session_id)
        return case_sessions.load_session(self.current_session_id)

    def delete_chat(self, session_id: str) -> dict:
        case_sessions.delete_session(session_id)
        if session_id == self.current_session_id:
            self._activate_most_recent_or_new()
        return {"current_session_id": self.current_session_id}

    def rename_chat(self, session_id: str, title: str) -> None:
        case_sessions.rename_session(session_id, title)

    def get_sessions_settings(self) -> dict:
        """Everything the Settings > Sessions tab shows - real, current
        storage numbers (not estimates) plus the saved retention setting."""
        info = case_sessions.get_storage_info()
        info["retention_days"] = case_agent.get_sessions_retention_days()
        return info

    def set_sessions_retention(self, days) -> str:
        case_agent.set_sessions_retention_days(days)
        return (
            f"Saved - chats older than {int(days)} day(s) will be deleted automatically on each CASE launch."
            if days and int(days) >= 1
            else "Saved - chats are kept forever (no auto-delete)."
        )

    def run_sessions_cleanup_now(self) -> str:
        """Manual, on-demand version of the same startup sweep - uses
        whatever retention setting is currently saved, so Save first if you
        just changed it and want to see the effect immediately. Guards the
        one real edge case a manual run (unlike the startup sweep) can hit:
        the chat you're currently LOOKING AT happens to be old enough to
        get swept - same fallback delete_chat() already uses so you're
        never left pointing at a session that no longer exists on disk."""
        retention_days = case_agent.get_sessions_retention_days()
        if not retention_days:
            return "No retention period set - nothing to clean up. Set one above first."
        deleted = case_sessions.delete_sessions_older_than(retention_days)
        if case_sessions.load_session(self.current_session_id) is None:
            self._activate_most_recent_or_new()
        if deleted == 0:
            return "No chats were old enough to delete."
        return f"Deleted {deleted} chat(s) older than {retention_days} day(s)."

    # --- Settings: Agent / system prompt ---
    def get_system_prompt(self) -> str:
        return case_agent.get_system_prompt()

    def get_default_system_prompt(self) -> str:
        return case_agent.CASE_SYSTEM_PROMPT

    def save_system_prompt(self, text: str) -> str:
        case_agent.set_system_prompt(text)
        self._apply_system_prompt_to_current_session()
        return "Saved."

    def reset_system_prompt(self) -> str:
        case_agent.reset_system_prompt()
        self._apply_system_prompt_to_current_session()
        return "Reset to default."

    def _apply_system_prompt_to_current_session(self):
        # Apply immediately to the live session (in memory AND on disk), not
        # just future new_history() calls for brand-new chats.
        if self.history and self.history[0].get("role") == "system":
            self.history[0]["content"] = case_agent.get_system_prompt()
            case_sessions.save_session(self.current_session_id, self.history)

    # --- Settings: Skills ---
    def list_skills(self) -> list:
        return case_skills.list_skills()

    def delete_skill(self, name: str) -> str:
        return case_skills.delete_skill(name)

    def find_importable_skills(self) -> list:
        return case_skills.find_importable_skills()

    def import_skill(self, name: str, description: str, triggers: list, body: str, mode: str = "keyword") -> str:
        return case_skills.import_skill(name, description, triggers, body, mode=mode)

    def suggest_triggers(self, description: str) -> list:
        return case_skills.suggest_triggers_from_description(description)

    def list_manual_skills(self) -> list:
        return case_skills.list_manual_skills()

    def pick_skill_file(self) -> str:
        """Native file picker for a raw .md file already in CASE's own
        skill format (frontmatter with name/description/triggers)."""
        if self._window is None:
            return "ERROR: window not ready."
        result = self._window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=False, file_types=("Markdown files (*.md)", "All files (*.*)"),
        )
        if not result:
            return ""  # cancelled
        return case_skills.import_skill_file(result[0])

    # --- Settings: model switcher ---
    def list_models(self) -> list:
        return case_agent.list_models()

    def get_current_model(self) -> str:
        # Real bug found 2026-09-13: this used to always return self.model
        # (the REMOTE backend's selected_model), completely unrelated to
        # local_model_path - so switching backend to "local" and picking a
        # specific .gguf via Settings > Local Models left the chat's own
        # model-name dropdown showing a stale, unrelated value (it couldn't
        # even highlight the right <option> since the strings didn't match
        # at all - list_models() returns local file LABELS in that mode,
        # not the remote-style id self.model held). Branch on the actual
        # current backend so this always reflects what's really answering.
        if case_agent.get_backend() == "local":
            path = case_agent.get_local_model_path()
            if not path:
                return "(no local model selected)"
            # Must return the SAME string list_models() uses as that file's
            # <option> value/text - for LM-Studio-sourced files that's the
            # full relative path (e.g. "publisher\model-folder\file.gguf"),
            # NOT the bare filename. Returning Path(path).name here (a real
            # bug caught by live-checking against the actual running app,
            # not just an isolated test) silently never matched any
            # <option>, so the browser fell back to highlighting whatever
            # option happened to sort first - a real, wrong-looking model
            # name with no error to indicate why.
            for entry in case_agent.list_local_model_files():
                if entry["path"] == path:
                    return entry["label"]
            return Path(path).name  # picked via Browse, outside both scanned folders - no matching <option> exists anyway
        return self.model

    def set_model(self, model_id: str) -> str:
        # Same local/remote split as get_current_model() above - the
        # dropdown's options ARE local file labels when backend=="local"
        # (see case_agent.list_models()), so model_id here is a label to
        # map back to a real path, not a remote-style model id.
        if case_agent.get_backend() == "local":
            for entry in case_agent.list_local_model_files():
                if entry["label"] == model_id:
                    case_agent.set_selected_local_model(entry["path"])
                    return entry["label"]
            return self.get_current_model()
        # Persisted (case_config.json), not just in-memory on this Api
        # instance - previously a restart silently reverted to the hardcoded
        # default with no indication that happened.
        self.model = model_id or case_agent.get_selected_model()
        case_agent.set_selected_model(self.model)
        return self.model

    # --- Settings: General (Bionic-native MCP write access) ---
    def get_mcp_write_enabled(self) -> bool:
        return case_agent.get_mcp_write_enabled()

    def save_mcp_write_enabled(self, enabled: bool) -> str:
        case_agent.set_mcp_write_enabled(enabled)
        return "Saved - restart Bionic (or reconnect the case-context integration) for this to take effect."

    # --- Settings: General (workspace/memory folder - shared by every
    # client, since they all import case_tools.py and it reads the same
    # case_config.json keys) ---
    def get_workspace_settings(self) -> dict:
        return {
            "project_root": str(case_tools.get_project_root()),
            "memory_root": str(case_tools.get_memory_root()),
        }

    def save_workspace_settings(self, project_root: str, memory_root: str) -> str:
        messages = []
        if project_root and project_root != str(case_tools.get_project_root()):
            messages.append(case_tools.set_project_root(project_root))
        if memory_root and memory_root != str(case_tools.get_memory_root()):
            messages.append(case_tools.set_memory_root(memory_root))
        return " ".join(messages) if messages else "No change."

    def reset_workspace_settings(self) -> str:
        r1 = case_tools.reset_project_root()
        r2 = case_tools.reset_memory_root()
        return f"{r1} {r2}"

    def pick_workspace_folder(self) -> str:
        """Native folder picker - returns the picked path, or '' if
        cancelled. Caller decides which field to fill in."""
        if self._window is None:
            return ""
        result = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return result[0] if result else ""

    # --- Settings: Local Models (backend + remote endpoint) ---
    def get_backend_settings(self) -> dict:
        return {
            "backend": case_agent.get_backend(),
            "remote_endpoint": case_agent.get_remote_base(),
            "local_models": case_agent.list_local_model_files(),
            "local_model_dir": str(case_agent.LOCAL_MODELS_DIR),
            "lmstudio_model_dir": str(case_agent.LMSTUDIO_MODELS_DIR),
            "selected_local_model": case_agent.get_local_model_path(),
            "subagent_enabled": case_agent.get_subagent_enabled(),
            "subagent_endpoint": case_agent.get_subagent_endpoint(),
            "subagent_model": case_agent.get_subagent_model(),
            "local_llm_status": case_agent.get_local_llm_status(),
        }

    def save_backend_settings(self, backend: str, remote_endpoint: str, subagent_enabled: bool = False,
                               subagent_endpoint: str = "", subagent_model: str = "") -> str:
        case_agent.set_backend(backend)
        case_agent.set_remote_endpoint(remote_endpoint or "")
        case_agent.set_subagent_enabled(subagent_enabled)
        case_agent.set_subagent_endpoint(subagent_endpoint or "")
        case_agent.set_subagent_model(subagent_model or "")
        if backend == "local" and not case_agent.get_local_model_path():
            return f"Saved - but no usable .gguf file found yet in {case_agent.LOCAL_MODELS_DIR} or {case_agent.LMSTUDIO_MODELS_DIR}."
        return "Saved."

    def select_local_model(self, path: str) -> str:
        return case_agent.set_selected_local_model(path)

    def reset_local_model_selection(self) -> str:
        return case_agent.reset_selected_local_model()

    def unload_local_model(self) -> str:
        return case_agent.unload_local_model()

    def get_model_info_summary(self) -> dict:
        """Everything the header's info popover shows - real, current
        values (not cached), so it never drifts from what's actually
        loaded. Shared core (backend/GPU/context) comes from
        case_agent.get_model_info_summary() - the same data the CLI's
        /model command reports - with model_label added on top, since only
        the GUI needs a string that exactly matches one of modelSelect's
        <option> values (see get_current_model()'s own docstring)."""
        info = case_agent.get_model_info_summary()
        info["model_label"] = self.get_current_model()
        return info

    def pick_local_model_file(self) -> str:
        """Native file picker for a .gguf ANYWHERE on disk - not just
        LOCAL_MODELS_DIR/LMSTUDIO_MODELS_DIR's auto-scanned locations.
        Real gap otherwise: a model kept on a different drive (e.g. D:)
        with no symlink/junction back into one of the scanned folders had
        no way to be selected except hand-editing case_config.json's
        local_model_path directly - this is that same override, exposed
        as an actual button. Returns set_selected_local_model's own real
        status message (not just the bare path) - e.g. it can fail if the
        picked file vanishes between the dialog closing and the save
        actually running - or "" if the dialog was cancelled."""
        if self._window is None:
            return ""
        result = self._window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=False,
            file_types=("GGUF model files (*.gguf)", "All files (*.*)"),
        )
        if not result:
            return ""
        return case_agent.set_selected_local_model(result[0])

    # --- Attachments ---
    def pick_attachment(self) -> dict | None:
        """Native OS file picker (Sati's own choice, not model-initiated) -
        see case_tools.read_file_for_attach for why this has no path
        restriction unlike the model-facing read_file tool."""
        if self._window is None:
            return None
        result = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False)
        if not result:
            return None
        return case_tools.read_file_for_attach(result[0])

    def send_message(self, text: str, allow_write: bool, attachment: dict = None, skill_name: str = None) -> dict:
        return self._run_turn(text, allow_write, attachment, skill_name)

    def _run_turn(self, text: str, allow_write: bool, attachment: dict = None, skill_name: str = None) -> dict:
        """The actual 'process one message' path - factored out so a voice
        transcript (see poll_voice_events below) runs through EXACTLY the
        same logic as a typed message: same attachment handling, same
        write-confirmation gate, same session save/auto-title. Voice input
        is just another way text arrives here, not a separate code path
        that could silently diverge (e.g. accidentally skip the confirm
        dialog). Returns {"answer": str, "reasoning": str | None} - the
        local backend's raw <think> block, when the model emitted one for
        this turn (see case_local_llm._strip_reasoning), so the GUI can
        show it in a collapsible section instead of just discarding it.
        skill_name: a mode: manual skill Sati explicitly picked for this
        message (see the 🎯 skill picker) - forced into context regardless
        of case_skills.match_skills()'s own keyword/semantic matching."""
        # First real exchange in a still-untitled chat -> derive a title from
        # the typed text only, never the (possibly huge) attached content.
        record = case_sessions.load_session(self.current_session_id)
        is_first_message = record is not None and record["title"] == case_sessions.DEFAULT_TITLE

        # Shared with case_cli.py's /attach command - see
        # case_agent.compose_message_with_attachment's own docstring for
        # why the exact wording matters (a real bug fix, not decoration).
        message = case_agent.compose_message_with_attachment(text, attachment)
        forced_skill_names = [skill_name] if skill_name else None

        if allow_write:
            try:
                answer = case_agent.ask(
                    message,
                    self.history,
                    allow_write=True,
                    confirm_callback=self._confirm,
                    model=self.model,
                    forced_skill_names=forced_skill_names,
                )
            except case_agent.CaseUnreachable as e:
                return {"answer": str(e), "reasoning": None}
            except Exception as e:
                return {"answer": f"Something went wrong: {type(e).__name__}: {e}", "reasoning": None}
        else:
            try:
                answer = case_agent.ask(message, self.history, model=self.model, forced_skill_names=forced_skill_names)
            except case_agent.CaseUnreachable as e:
                return {"answer": str(e), "reasoning": None}
            except Exception as e:
                return {"answer": f"Something went wrong: {type(e).__name__}: {e}", "reasoning": None}

        title = case_sessions.auto_title_from(text) if is_first_message else None
        case_sessions.save_session(self.current_session_id, self.history, title=title)
        reasoning = self.history[-1].get("reasoning") if self.history else None
        return {"answer": answer, "reasoning": reasoning}

    def _confirm(self, fn_name: str, args: dict) -> bool:
        """Called by case_agent.ask() before write_file/delete_file actually
        runs. Shows a real native OS confirmation dialog and blocks until
        Sati answers - this IS the standalone confirmation gate, the whole
        reason write access is safe to offer outside Bionic's own UI."""
        if self._window is None:
            return False  # fail closed if somehow called before the window exists

        if fn_name == "write_file":
            path = args.get("path", "?")
            mode = args.get("mode", "overwrite")
            content = args.get("content", "")
            preview = content[:300] + ("..." if len(content) > 300 else "")
            message = (
                f"CASE wants to {mode} this file:\n\n{path}\n\n"
                f"Content preview:\n{preview}"
            )
            title = "Confirm: write_file"
        elif fn_name == "delete_file":
            path = args.get("path", "?")
            message = f"CASE wants to DELETE this file:\n\n{path}\n\n(a backup of its current content is kept - use restore_last_backup to undo)"
            title = "Confirm: delete_file"
        elif fn_name == "restore_last_backup":
            path = args.get("path", "?")
            message = f"CASE wants to UNDO the last change to this file by restoring its most recent backup:\n\n{path}"
            title = "Confirm: restore_last_backup"
        else:
            message = f"CASE wants to run: {fn_name}({args})"
            title = "Confirm tool call"

        return self._window.create_confirmation_dialog(title, message)

    # --- Voice (Settings > Voice) ---
    def get_voice_settings(self) -> dict:
        return {
            "deps_available": case_voice.voice_deps_available(),
            "devices": case_voice.list_input_devices(),
            "config": case_voice.get_voice_config(),
            "status": case_voice.get_status(),
        }

    def pick_voice_ref_clip(self) -> str:
        """Native file picker for a reference .wav - same pattern as
        pick_attachment, but this just needs the path (F5-TTS reads the
        file itself), not file content."""
        if self._window is None:
            return ""
        result = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False)
        return result[0] if result else ""

    def save_voice_config(self, mic_device, ref_clip: str, ref_text: str) -> str:
        case_voice.set_voice_config(mic_device=mic_device, ref_clip=ref_clip, ref_text=ref_text)
        return "Saved."

    def start_voice_listening(self, mic_device=None) -> str:
        return case_voice.start_listening(mic_device)

    def stop_voice_listening(self) -> str:
        return case_voice.stop_listening()

    def set_voice_speaking_enabled(self, enabled: bool) -> None:
        case_voice.set_speaking_enabled(enabled)

    def get_voice_status(self) -> dict:
        return case_voice.get_status()

    def poll_voice_events(self, allow_write: bool) -> list:
        """Called on a short JS interval (see pollVoiceEvents in the HTML)
        while the chat is open. Drains anything the mic heard since the
        last poll and runs each transcript through the EXACT SAME turn
        logic as a typed message (_run_turn) - a voice utterance is not a
        second, divergent code path. Returns [{"user": ..., "assistant": ...}]
        for the JS to append to the chat log; also queues the reply for TTS
        playback if speaking is enabled. Safe to call even when voice was
        never started - drain_transcripts() just returns an empty list."""
        pairs = []
        for text in case_voice.drain_transcripts():
            result = self._run_turn(text, allow_write, attachment=None)
            case_voice.speak(result["answer"])
            pairs.append({"user": text, "assistant": result["answer"], "reasoning": result["reasoning"]})
        return pairs

    # --- Browser panel inside this window (see case_browser_panel.py). Thin
    # pass-throughs: the JS side polls panel_frame and forwards clicks/keys.
    def panel_available(self) -> bool:
        return case_browser_panel.available()

    def panel_frame(self, last_hash: str = "") -> dict:
        return case_browser_panel.frame(last_hash)

    def panel_poll(self) -> dict:
        return case_browser_panel.poll()

    def panel_set_viewport(self, width, height) -> dict:
        return case_browser_panel.set_viewport(width, height)

    def panel_click(self, x, y, button="left", clicks=1) -> dict:
        return case_browser_panel.click(x, y, button, clicks)

    def panel_scroll(self, x, y, dx, dy) -> dict:
        return case_browser_panel.scroll(x, y, dx, dy)

    def panel_key(self, key_name, ctrl=False, shift=False, alt=False) -> dict:
        return case_browser_panel.key(key_name, ctrl, shift, alt)

    def panel_insert_text(self, text) -> dict:
        return case_browser_panel.insert_text(text)

    def panel_navigate(self, text) -> dict:
        return case_browser_panel.navigate(text)

    def panel_history(self, step) -> dict:
        return case_browser_panel.history(step)

    def panel_reload(self) -> dict:
        return case_browser_panel.reload()

    def panel_new_tab(self, url="") -> dict:
        return case_browser_panel.new_tab(url)

    def panel_switch_tab(self, tab_id) -> dict:
        return case_browser_panel.switch_tab(tab_id)

    def panel_close_tab(self, tab_id) -> dict:
        return case_browser_panel.close_tab(tab_id)

    def panel_ensure_width(self, min_width) -> None:
        """Widen the CASE window so the panel has room beside the chat."""
        try:
            win = self._window
            if win.width < min_width:
                win.resize(int(min_width), win.height)
        except Exception:
            pass

    def open_browser(self, url: str) -> str:
        result = case_browser.open_browser_window(url)
        # Same reasoning as case_gui.py's button: log a note into history so
        # CASE has context if asked about it later, since this bypasses the
        # model entirely for instant access.
        self.history.append({
            "role": "user",
            "content": (
                f"[The user just clicked the Browser button directly in the GUI - "
                f"{result} You did not open this yourself and can't see its "
                f"live content, but you should know it exists if asked about it.]"
            ),
        })
        case_sessions.save_session(self.current_session_id, self.history)
        return result


def main():
    if not _acquire_single_instance_lock():
        _focus_existing_window()
        return

    # The browser lives in the panel inside this window - run it headless so
    # it never opens a window of its own.
    case_browser.EMBEDDED = True

    api = Api()
    window = webview.create_window(
        WINDOW_TITLE,
        str(HTML_FILE),
        width=880,
        height=820,
        min_size=(480, 500),
        js_api=api,
        background_color="#1a1d1e",
    )
    api._window = window
    # debug=True is what actually turns on right-click (pywebview ties
    # AreDefaultContextMenusEnabled to debug mode on the WebView2 backend -
    # confirmed via reading edgechromium.py) - without it, right-click does
    # nothing at all, no copy/paste, no way to select text out of the window.
    # OPEN_DEVTOOLS_IN_DEBUG defaults to True and auto-pops a separate
    # DevTools window on every launch as a side effect of debug=True - off
    # here since only the context menu itself was wanted, not an extra
    # window every time. F12/right-click-Inspect still work on demand.
    webview.settings['OPEN_DEVTOOLS_IN_DEBUG'] = False
    # Lets a real Chrome DevTools Protocol client (including Claude's own
    # Browser tool) attach to THIS actual running native window - not a
    # disconnected browser tab loading the same URL separately.
    #
    # IMPORTANT, found 2026-09-13 while chasing a real "double-click does
    # nothing" report: this setting was WRONGLY suspected first (it briefly
    # got removed here) - direct Win32 IsWindowVisible/EnumWindows
    # inspection proved debug=True and REMOTE_DEBUGGING_PORT are each
    # innocent alone AND together, when launched directly (python.exe or
    # pythonw.exe invoked straight). The REAL cause was launch_case.vbs's
    # own WshShell.Run(..., 0, False) - WindowStyle 0 = SW_HIDE, which
    # apparently now propagates as a "start hidden" hint to pywebview's
    # WinForms window itself (not just suppressing a console window,
    # which pythonw.exe never has anyway) - see launch_case.vbs's own
    # comment for the actual fix. Confirmed via repeated, controlled
    # isolation: identical code, visible when launched directly, invisible
    # only when launched through the unfixed vbs, regardless of these two
    # settings. Restored here since removing them fixed nothing.
    webview.settings['REMOTE_DEBUGGING_PORT'] = 8228
    webview.start(debug=True)


if __name__ == "__main__":
    main()
