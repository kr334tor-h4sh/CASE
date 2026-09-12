This folder is CASE's own LAST-RESORT memory fallback - used only when the
shared memory system (TARS/Claude Code's own `memory_engine.py`/`memory.db`
under `~/.claude/`) is unreachable: missing entirely, crashed, or timed out.

Why this exists: `memory_reflect` deliberately reads from the SAME memory
TARS/Claude Code uses, by design - CASE knowing exactly what TARS knows
(not a separate, drifting copy) is the whole point of "helpful if
something happens to Claude Code." That covers the common failure modes
fine (the Claude Code app breaking, or the Anthropic API being down don't
touch `~/.claude`'s local files at all - `memory_reflect` shells out to a
pure local script, no dependency on either). The one real gap: if the PC's
`~/.claude` folder itself were lost (disk failure, accidental deletion,
OS reinstall), CASE's memory would break too, with nothing to fall back
on. This folder closes that gap, cheaply - a plain keyword search over a
few local markdown files, not a duplicate semantic-search/sentence-
transformers setup (heavy, redundant, and CASE's own narrow fallback
content doesn't need it).

**This WILL go stale** - it's a snapshot, not a live copy, and there is no
automatic sync. Whenever `memory_reflect` actually falls back to searching
here, its answer explicitly says so, so it's never mistaken for current,
live information. Worth manually refreshing occasionally (ask CASE or
TARS to re-copy the real MEMORY.md content here) if this ever actually
gets used for real - a stale-but-labeled fallback is still better than a
hard failure in the one scenario this exists for.
