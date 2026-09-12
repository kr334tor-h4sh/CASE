"""
Local, file-based chat history + projects for CASE's GUI - the same shape
of thing Bionic's own "Projects" / "Chats" sidebar gives you, since
case_gui_web.py previously kept only one in-memory conversation that
vanished when the window closed.

Storage: CASE/case_sessions/_index.json (fast list of projects + session
metadata) plus one CASE/case_sessions/<id>.json per session (full message
history in the same OpenAI-message shape case_agent.py already uses).
Plain JSON, no database - consistent with the rest of this project's
"no unnecessary dependencies" approach, and small enough that this is fine.

This module only manages storage - it knows nothing about the model, the
system prompt, or tool-calling. case_gui_web.py wires it together with
case_agent.py.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

SESSIONS_DIR = Path(__file__).parent / "case_sessions"
INDEX_PATH = SESSIONS_DIR / "_index.json"

DEFAULT_TITLE = "New chat"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dir() -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, data) -> None:
    _ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)  # atomic on Windows for same-volume renames


def _load_index() -> dict:
    if not INDEX_PATH.exists():
        return {"projects": [], "sessions": []}
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"projects": [], "sessions": []}


def _save_index(index: dict) -> None:
    _atomic_write_json(INDEX_PATH, index)


def _session_path(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}.json"


def list_projects() -> list:
    return list(_load_index()["projects"])


def create_project(name: str) -> dict:
    index = _load_index()
    record = {"id": str(uuid.uuid4()), "name": name.strip() or "Untitled project", "created_at": _now()}
    index["projects"].append(record)
    _save_index(index)
    return record


def delete_project(project_id: str) -> None:
    """Deletes the project itself; sessions that belonged to it are kept,
    just un-assigned (moved back to no project) rather than deleted."""
    index = _load_index()
    index["projects"] = [p for p in index["projects"] if p["id"] != project_id]
    affected_ids = []
    for s in index["sessions"]:
        if s.get("project_id") == project_id:
            s["project_id"] = None
            affected_ids.append(s["id"])
    _save_index(index)
    # Each session's own file carries its own project_id copy too - the
    # index update above isn't enough, load_session() reads the file directly.
    for session_id in affected_ids:
        record = load_session(session_id)
        if record is not None:
            record["project_id"] = None
            _atomic_write_json(_session_path(session_id), record)


def list_all_sessions() -> list:
    """Most-recently-updated first."""
    sessions = list(_load_index()["sessions"])
    sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
    return sessions


def list_sessions_by_project(project_id) -> list:
    return [s for s in list_all_sessions() if s.get("project_id") == project_id]


def create_session(history: list, project_id=None, title: str = DEFAULT_TITLE) -> dict:
    session_id = str(uuid.uuid4())
    now = _now()
    record = {
        "id": session_id,
        "title": title,
        "project_id": project_id,
        "created_at": now,
        "updated_at": now,
        "history": history,
    }
    _atomic_write_json(_session_path(session_id), record)

    index = _load_index()
    index["sessions"].append({
        "id": session_id, "title": title, "project_id": project_id,
        "created_at": now, "updated_at": now,
    })
    _save_index(index)
    return record


def load_session(session_id: str):
    path = _session_path(session_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_session(session_id: str, history: list, title=None) -> None:
    record = load_session(session_id)
    if record is None:
        return
    record["history"] = history
    record["updated_at"] = _now()
    if title is not None:
        record["title"] = title
    _atomic_write_json(_session_path(session_id), record)

    index = _load_index()
    for s in index["sessions"]:
        if s["id"] == session_id:
            s["updated_at"] = record["updated_at"]
            if title is not None:
                s["title"] = title
            break
    _save_index(index)


def rename_session(session_id: str, title: str) -> None:
    record = load_session(session_id)
    if record is None:
        return
    title = title.strip() or DEFAULT_TITLE
    record["title"] = title
    _atomic_write_json(_session_path(session_id), record)
    index = _load_index()
    for s in index["sessions"]:
        if s["id"] == session_id:
            s["title"] = title
            break
    _save_index(index)


def set_session_project(session_id: str, project_id) -> None:
    record = load_session(session_id)
    if record is None:
        return
    record["project_id"] = project_id
    _atomic_write_json(_session_path(session_id), record)
    index = _load_index()
    for s in index["sessions"]:
        if s["id"] == session_id:
            s["project_id"] = project_id
            break
    _save_index(index)


def delete_session(session_id: str) -> None:
    path = _session_path(session_id)
    if path.exists():
        path.unlink()
    index = _load_index()
    index["sessions"] = [s for s in index["sessions"] if s["id"] != session_id]
    _save_index(index)


def get_storage_info() -> dict:
    """Real, current numbers for the Settings > Sessions tab - not an
    estimate. Counts every *.json file actually on disk under
    SESSIONS_DIR (the index file itself excluded, same set list_all_sessions()
    would report on) so this stays honest even if the index and the real
    files on disk ever drift."""
    _ensure_dir()
    total_bytes = 0
    count = 0
    for p in SESSIONS_DIR.glob("*.json"):
        if p.name == INDEX_PATH.name:
            continue
        try:
            total_bytes += p.stat().st_size
            count += 1
        except OSError:
            continue
    return {"count": count, "total_bytes": total_bytes, "dir": str(SESSIONS_DIR)}


def delete_sessions_older_than(days: int) -> int:
    """Deletes every session whose OWN updated_at (last real activity, not
    creation time - a chat you keep coming back to should never age out
    just because it's old) is more than `days` days in the past. Returns
    how many were actually deleted. A brand new/just-active session is
    never at risk here regardless of call order - its updated_at is always
    "now", so it can't already be older than any positive `days` cutoff."""
    if not days or days < 1:
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
    deleted = 0
    for s in list_all_sessions():
        try:
            updated_ts = datetime.fromisoformat(s["updated_at"]).timestamp()
        except (KeyError, ValueError):
            continue
        if updated_ts < cutoff:
            delete_session(s["id"])
            deleted += 1
    return deleted


def auto_title_from(text: str) -> str:
    text = " ".join(text.split())  # collapse whitespace/newlines
    return text[:40] + ("…" if len(text) > 40 else "")
