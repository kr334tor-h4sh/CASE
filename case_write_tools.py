"""
Write/modify tools for CASE - wired into whichever client has a REAL
confirmation mechanism before a write/delete actually executes:
- case_mcp_server.py (Bionic's own native chat UI) - Bionic's built-in
  tool-confirmation prompt (~/.lmstudio/settings.json ->
  chat.neverAskForToolConfirmation = false).
- case_gui_web.py (2026-09-12) - via case_agent.ask(allow_write=True,
  confirm_callback=...), gated by its own native OS confirmation dialog
  (pywebview's Window.create_confirmation_dialog).

Deliberately NEVER wired into case_cli.py (the terminal client)
- it executes tool calls immediately with no confirmation mechanism at
all, so giving it write access would mean an unchecked model modifying
files with nothing standing in the way. Any new client MUST supply a real
confirmation gate before being handed these tools - see case_agent.ask()'s
allow_write/confirm_callback parameters, which enforce this (raises if
allow_write=True with no confirm_callback).

Scope: same two roots as the read-only tools (project files + memory
folder), same path-traversal protection. Text files only - no binary
writes, no arbitrary code execution.
"""

from datetime import datetime
from pathlib import Path

from case_tools import _resolve_safe, get_allowed_roots, get_project_root, get_memory_root

MAX_WRITE_BYTES = 500_000  # ~500KB cap per write, sanity limit

# Lightweight, git-free reversibility: back up a file's content before an
# overwrite or delete destroys it. The confirmation dialog is the primary
# safety gate (Sati sees the change before it happens), but a bad edit or a
# mis-typed path is still one click away without this - and unlike Aider's
# auto-commit-per-edit, CASE's project folder isn't a git repo, so this is
# the equivalent safety net without requiring one.
BACKUP_DIR = Path(__file__).parent / "case_backups"
MAX_BACKUPS_PER_FILE = 5  # keep it bounded - oldest pruned, not unlimited growth


def _backup(target: Path) -> str | None:
    """Copy target's current content into BACKUP_DIR before it's overwritten
    or deleted. Returns the backup path, or None if there was nothing to
    back up (file doesn't exist yet) or the backup itself failed - a failed
    backup never blocks the real write/delete, it just means no safety net
    for that one operation."""
    if not target.exists():
        return None
    try:
        BACKUP_DIR.mkdir(exist_ok=True)
        # A pure integer sequence, NOT a wall-clock timestamp, is what
        # actually guarantees correct ordering here. Tried timestamp-based
        # naming first (seconds, then microseconds, then microseconds +
        # exists()-check-and-increment) - all three still produced an
        # intermittent (not every run) wrong-file-pruned/wrong-file-selected
        # flake in test_case_backup.py under this exact OneDrive-synced
        # folder: once an older same-timestamp file gets pruned, a LATER
        # call can legitimately regenerate that identical timestamp string
        # (the wall clock's effective resolution is coarser than this
        # write rate) and its collision check finds nothing to collide
        # with anymore, silently reusing an "early-sorting" name for
        # content that's actually the newest. An always-increasing integer
        # can't be recycled this way - see _next_backup_seq.
        seq = _next_backup_seq(target.name)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")  # human-readable only, not used for ordering
        backup_path = BACKUP_DIR / f"{target.name}.{seq:010d}.{stamp}.bak"
        backup_path.write_bytes(target.read_bytes())
        _prune_old_backups(target.name)
        return str(backup_path)
    except OSError:
        return None


def _next_backup_seq(original_name: str) -> int:
    """One higher than the highest sequence number among this file's
    existing backups (0 if none exist yet). Safe without locking: CASE's
    write/delete tools only ever run synchronously, one confirmed action
    at a time - never called concurrently from multiple threads/processes."""
    prefix = f"{original_name}."
    max_seq = -1
    for p in BACKUP_DIR.glob(f"{original_name}.*.bak"):
        seq_part = p.name[len(prefix):].split(".", 1)[0]
        if seq_part.isdigit():
            max_seq = max(max_seq, int(seq_part))
    return max_seq + 1


def _prune_old_backups(original_name: str) -> None:
    # Sort by filename, which now sorts numerically (zero-padded integer
    # sequence prefix) rather than by any clock - see _backup/_next_backup_seq
    # for why a wall-clock-based scheme was tried and dropped.
    matches = sorted(BACKUP_DIR.glob(f"{original_name}.*.bak"))
    for stale in matches[:-MAX_BACKUPS_PER_FILE]:
        try:
            stale.unlink()
        except OSError:
            pass


def write_file(path: str, content: str, mode: str = "overwrite") -> str:
    """Write text content to a file under the allowed project/memory
    folders. mode='overwrite' (default) replaces the whole file, mode='append'
    adds to the end. Creates the file (and any needed parent folders) if it
    doesn't exist yet. Sati will be asked to confirm before this actually
    runs - do not assume it silently succeeded without seeing that."""
    if mode not in ("overwrite", "append"):
        return f"ERROR: mode must be 'overwrite' or 'append', got '{mode}'."
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_WRITE_BYTES:
        return f"ERROR: content is {len(encoded):,} bytes, over the {MAX_WRITE_BYTES:,} byte limit for one write."

    # resolve against allowed roots even for a path that doesn't exist yet
    project_root = get_project_root()
    candidate = Path(path)
    if candidate.is_absolute():
        target = candidate.resolve()
        allowed = any(_is_within(target, root) for root in get_allowed_roots())
        if not allowed:
            return f"ERROR: '{path}' is outside the allowed folders ({project_root} or {get_memory_root()})."
    else:
        target = (project_root / candidate).resolve()
        if not _is_within(target, project_root):
            return f"ERROR: '{path}' resolves outside the allowed project folder."

    # Only overwrite destroys prior content - append never loses anything,
    # so no backup needed there.
    backup_path = _backup(target) if mode == "overwrite" else None

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        write_mode = "a" if mode == "append" else "w"
        with open(target, write_mode, encoding="utf-8") as f:
            f.write(content)
    except PermissionError:
        return f"ERROR: permission denied writing '{path}'."
    except OSError as e:
        return f"ERROR: could not write '{path}': {e}"

    verb = "Appended to" if mode == "append" else "Wrote"
    result = f"{verb} {target} ({len(encoded):,} bytes)."
    if backup_path:
        result += f" Previous content backed up to {backup_path}."
    return result


def delete_file(path: str) -> str:
    """Delete a single file under the allowed project/memory folders.
    Does NOT delete directories (refuses, to avoid an accidental recursive
    wipe). Sati will be asked to confirm before this actually runs."""
    try:
        target = _resolve_safe(path)
    except PermissionError as e:
        return f"ERROR: {e}"
    if not target.exists():
        return f"ERROR: '{path}' does not exist - nothing to delete."
    if target.is_dir():
        return f"ERROR: '{path}' is a directory - refusing to delete directories (too easy to wipe more than intended). Delete specific files instead."
    backup_path = _backup(target)
    try:
        target.unlink()
    except PermissionError:
        return f"ERROR: permission denied deleting '{path}'."
    result = f"Deleted {target}."
    if backup_path:
        result += f" Content backed up to {backup_path}."
    return result


def restore_last_backup(path: str) -> str:
    """Undo the most recent write_file(overwrite)/delete_file on this file
    by restoring its latest backup - CASE's equivalent of Claude Code's
    checkpoint/undo. Only ever looks at THIS file's own backups (never
    guesses at a different file). Goes through write_file() to actually
    apply the restored content, which means it's bounded by the exact same
    MAX_WRITE_BYTES/path-safety checks as a normal write, AND - since
    write_file() itself backs up whatever's there right now before
    overwriting - the state right before this undo is itself saved. Call
    this again and it undoes the undo (restores that just-created backup),
    same MAX_BACKUPS_PER_FILE-deep safety net as everything else here, not
    a separate redo feature. Sati will be asked to confirm before this
    actually runs, same as write_file/delete_file."""
    try:
        target = _resolve_safe(path)
    except PermissionError as e:
        return f"ERROR: {e}"
    backups = sorted(BACKUP_DIR.glob(f"{target.name}.*.bak"))
    if not backups:
        return f"ERROR: no backup found for '{path}' - nothing to restore."
    latest = backups[-1]
    try:
        content = latest.read_text(encoding="utf-8")
    except OSError as e:
        return f"ERROR: could not read backup '{latest}': {e}"
    result = write_file(path, content, mode="overwrite")
    return f"Restored '{path}' from its most recent backup ({latest.name}). {result}"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write or append text content to a file under the user's project/memory folders. Creates the file and parent folders if needed. A real confirmation prompt shows the exact path/content and blocks before anything happens - that is the safety gate, not your own caution. If the user's request is vague (no exact path/content given), don't just ask for clarification - propose a concrete path and content yourself and call this tool; they review and can reject it in the confirmation prompt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "mode": {"type": "string", "enum": ["overwrite", "append"], "description": "Default 'overwrite'."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a single file under the user's project/memory folders. Cannot delete directories. A real confirmation prompt shows the exact path and blocks before anything happens. Only ask for clarification first if which file is meant is genuinely ambiguous between multiple real candidates - otherwise call this directly with the file the user clearly meant and let the confirmation prompt be the check.",
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
            "name": "restore_last_backup",
            "description": "Undo the most recent write_file/delete_file on a file by restoring its latest automatic backup (CASE backs up a file's prior content before every overwrite or delete). Use when the user asks to undo/revert/restore a recent change. Restores the SINGLE most recent backup only - not a specific point further back in history. A real confirmation prompt shows the exact path and blocks before anything happens.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "write_file": write_file,
    "delete_file": delete_file,
    "restore_last_backup": restore_last_backup,
}
