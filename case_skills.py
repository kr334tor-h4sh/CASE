"""
Lightweight skills for CASE - markdown files with YAML frontmatter under
CASE/skills/, matched against the user's message and injected into context
only for turns where they're relevant, instead of every scenario being
baked permanently into CASE_SYSTEM_PROMPT.

Matching is deliberately NOT a model tool-call the model decides to make: a
small local model is far more reliable at just answering a question than
at correctly judging "should I go fetch a skill file right now" as a
separate meta-decision - and Claude Code's own docs confirm even ITS
purely-model-driven invocation needs every skill description loaded into
context on every turn (a real, recurring token cost) plus dedicated
tooling (`/skill-doctor`) to keep it reliable, running on a frontier model
with a much bigger context window than CASE's local backend has to work
with. So skill selection stays deterministic here, in code - three modes,
chosen per skill (frontmatter `mode:`, 2026-09-12):
- keyword (default, CASE's original approach): plain substring match
  against explicit trigger phrases - cheap, fully predictable.
- semantic: local embedding similarity between the skill's OWN description
  and the message - no triggers to write, catches paraphrases, still
  entirely local/deterministic (see match_skills()).
- manual: never auto-fires - invoked explicitly instead (see
  list_manual_skills() / case_agent.ask()'s forced_skill_names) - CASE's
  answer to Claude Code's `disable-model-invocation: true`, for a skill
  where good triggers/description-similarity just aren't reliable enough.

Inspired by (see case_agent_project.md memory for the full research):
OpenClaw's Skills (gated markdown, precedence layers, loaded on demand),
OpenCode's `skill` tool + AGENTS.md convention, Goose's Skills/Hooks - and
the discovery that Bionic itself is already configured to read skills from
~/.claude/skills (a different format/scope than this - Claude Code's own
skills reference tools and conventions CASE doesn't have, so CASE gets its
own small, purpose-built set here rather than trying to reuse those).
"""

import re
from pathlib import Path

SKILLS_DIR = Path(__file__).parent / "skills"
MAX_SKILLS_PER_TURN = 2  # cap context growth even if several triggers fire at once

# Where to look for importable skills already on the device - Claude Code's
# own global skills folder, confirmed (2026-09-13, live settings.json
# inspection; 2026-09-12, confirmed Bionic filters on frontmatter presence
# by directly comparing its shown list against what's really on disk) to
# be the same one Bionic itself already reads from. Their format has no
# `triggers` field (Claude Code lets the model judge relevance from the
# description instead, at real per-turn token cost - see module docstring)
# - importing into CASE offers keyword mode (triggers suggested from the
# description's own quoted examples via suggest_triggers_from_description,
# still reviewed/editable by Sati) alongside semantic/manual modes for
# descriptions that don't have clean example phrases to extract.
CLAUDE_SKILLS_SEARCH_PATHS = [Path.home() / ".claude" / "skills"]

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse_yaml_frontmatter_lines(frontmatter_text: str) -> dict:
    """Hand-rolled, deliberately minimal YAML frontmatter parser - plain
    `key: value` lines, PLUS block scalars (`key: >` folded / `key: |`
    literal, followed by indented continuation lines). Not a general YAML
    parser (no lists-of-dicts, anchors, etc.) - just enough for the flat
    shape every real skill file here uses. Real bug caught live
    2026-09-12: without block-scalar support, financial-research's
    multi-line `description: >` silently became the literal 1-character
    string '>' - no crash, so it sat unnoticed until it rendered wrong
    (truncated to '>') in the Skills settings tab's import-scan list."""
    lines = frontmatter_text.splitlines()
    meta = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if ":" not in line or line[:1] in (" ", "\t"):
            i += 1
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value in (">", "|", ">-", ">+", "|-", "|+"):
            fold = value.startswith(">")
            i += 1
            block_lines = []
            while i < len(lines) and (not lines[i].strip() or lines[i][:1] in (" ", "\t")):
                block_lines.append(lines[i])
                i += 1
            indents = [len(l) - len(l.lstrip()) for l in block_lines if l.strip()]
            cut = min(indents) if indents else 0
            dedented = [l[cut:] if l.strip() else "" for l in block_lines]
            text = "\n".join(dedented)
            meta[key] = re.sub(r"\n(?!\n)", " ", text).strip() if fold else text.strip()
            continue
        meta[key] = value
        i += 1
    return meta


def _parse_skill_file(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    frontmatter_text, body = m.groups()
    meta = _parse_yaml_frontmatter_lines(frontmatter_text)
    if "triggers" in meta:
        # triggers: [write, delete, save a file]
        meta["triggers"] = [t.strip().strip("\"'") for t in meta["triggers"].strip("[]").split(",") if t.strip()]
    else:
        meta["triggers"] = []
    mode = meta.get("mode") or "keyword"
    if mode not in ("keyword", "semantic", "manual"):
        mode = "keyword"
    meta["mode"] = mode
    if "name" not in meta:
        return None
    # keyword mode is the only one that NEEDS triggers - semantic mode
    # matches on the description itself (see _semantic_match), manual mode
    # never auto-fires at all (see list_manual_skills). A keyword-mode
    # skill with no triggers would just silently never fire, so that one
    # case is still rejected outright.
    if mode == "keyword" and not meta["triggers"]:
        return None
    meta["body"] = body.strip()
    meta["_path"] = str(path)
    return meta


def list_skills() -> list:
    """All valid skill files found in SKILLS_DIR, parsed. Skips (silently)
    any file missing required frontmatter rather than crashing on one bad
    file - a malformed skill just doesn't fire, it doesn't break CASE."""
    if not SKILLS_DIR.exists():
        return []
    skills = []
    for p in sorted(SKILLS_DIR.glob("*.md")):
        parsed = _parse_skill_file(p)
        if parsed:
            skills.append(parsed)
    return skills


SEMANTIC_MATCH_THRESHOLD = 0.5  # empirically checked 2026-09-12 against BAAI/bge-small-en-v1.5:
# real relevant messages scored 0.52-0.62 against a skill description,
# real irrelevant ones scored 0.34-0.40 - clean gap, not a guessed number.

_EMBEDDING_MODEL_NAME = "BAAI/bge-small-en-v1.5"  # same model memory_engine.py already
# uses for memory search - reusing it means no second model download, and
# it's already proven to work well for this kind of short-text relevance
# matching in this exact project.
_embedder = None  # lazy singleton, same pattern as case_local_llm._get_llm()
_SEMANTIC_CACHE_PATH = SKILLS_DIR / "_semantic_cache.json"


def _get_embedder():
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer(_EMBEDDING_MODEL_NAME)
    return _embedder


def _cosine_similarity(a, b) -> float:
    import numpy as np
    a, b = np.array(a), np.array(b)
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1e-9
    return float(np.dot(a, b) / denom)


def _load_semantic_cache() -> dict:
    if not _SEMANTIC_CACHE_PATH.exists():
        return {}
    try:
        import json
        return json.loads(_SEMANTIC_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_semantic_cache(cache: dict) -> None:
    try:
        import json
        SKILLS_DIR.mkdir(exist_ok=True)
        _SEMANTIC_CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


def _get_description_embedding(skill: dict, cache: dict) -> list:
    """Cached on disk, keyed by skill name + a hash of its own description -
    so an edited description gets re-embedded automatically, but re-running
    match_skills() every single turn (the normal case) doesn't re-embed
    anything that hasn't changed. Embedding the incoming user message still
    happens fresh every call - only the skill side is cacheable, since the
    message is different every time."""
    import hashlib
    description = skill.get("description", "")
    desc_hash = hashlib.md5(description.encode("utf-8")).hexdigest()
    entry = cache.get(skill["name"])
    if entry and entry.get("hash") == desc_hash:
        return entry["vector"]
    vector = _get_embedder().encode(description, normalize_embeddings=True).tolist()
    cache[skill["name"]] = {"hash": desc_hash, "vector": vector}
    return vector


def _semantic_match(skill: dict, user_text: str, cache: dict) -> bool:
    skill_vec = _get_description_embedding(skill, cache)
    user_vec = _get_embedder().encode(user_text, normalize_embeddings=True).tolist()
    return _cosine_similarity(skill_vec, user_vec) >= SEMANTIC_MATCH_THRESHOLD


def match_skills(user_text: str) -> list:
    """Skills relevant to this turn, capped at MAX_SKILLS_PER_TURN: keyword
    matches first (file-listing order), then semantic matches best score first. Three modes, chosen per-skill at import/creation
    time (frontmatter `mode:`, defaults to keyword):
    - keyword (default, CASE's original approach): a real case-insensitive
      substring match against user_text - cheap, fully deterministic,
      needs Sati to have written trigger phrases.
    - semantic: cosine similarity between the skill's OWN description and
      user_text, via a local embedding model (see module-level constants
      above) - no triggers needed, catches paraphrases a substring match
      would miss, still fully local and still doesn't ask the (small,
      less reliable at this kind of meta-judgment) chat model itself to
      decide relevance.
    - manual: never auto-matched here at all - see list_manual_skills()
      and case_agent.ask()'s forced_skill_names param for how Sati invokes
      one of these explicitly instead."""
    text_lower = user_text.lower()
    keyword_hits = []
    semantic_hits = []  # (score, skill)
    semantic_cache = None
    user_vec = None
    cache_dirty = False
    for skill in list_skills():
        mode = skill.get("mode", "keyword")
        if mode == "manual":
            continue
        elif mode == "semantic":
            if semantic_cache is None:
                semantic_cache = _load_semantic_cache()
            skill_vec = _get_description_embedding(skill, semantic_cache)
            cache_dirty = True  # _get_description_embedding() may have mutated semantic_cache in-place
            if user_vec is None:
                user_vec = _get_embedder().encode(user_text, normalize_embeddings=True).tolist()
            score = _cosine_similarity(skill_vec, user_vec)
            if score >= SEMANTIC_MATCH_THRESHOLD:
                semantic_hits.append((score, skill))
        else:
            if any(trigger.lower() in text_lower for trigger in skill["triggers"]):
                keyword_hits.append(skill)
    if cache_dirty and semantic_cache is not None:
        _save_semantic_cache(semantic_cache)
    # An explicit keyword trigger beats a fuzzy similarity score, and among
    # semantic hits the best score wins - NOT file-listing order, which let
    # loosely-worded semantic skills (found 2026-10-01) fill both slots
    # before a keyword skill that actually named the request was reached.
    semantic_hits.sort(key=lambda pair: pair[0], reverse=True)
    matched = keyword_hits + [skill for _, skill in semantic_hits]
    return matched[:MAX_SKILLS_PER_TURN]


def list_manual_skills() -> list:
    """Skills with mode: manual - never auto-fire, only invoked explicitly
    (see case_agent.ask()'s forced_skill_names). Used by the GUI's "use a
    skill" picker."""
    return [s for s in list_skills() if s.get("mode") == "manual"]


def get_skill_by_name(name: str) -> dict | None:
    for skill in list_skills():
        if skill["name"] == name:
            return skill
    return None


_QUOTED_PHRASE_RE = re.compile(r'"([^"]{3,60})"|\'([^\']{3,60})\'')


def suggest_triggers_from_description(description: str) -> list:
    """Real, observed pattern (2026-09-12): every Claude-Code-format skill
    description checked so far that documents its own auto-trigger phrases
    does so as a quoted list ('Auto-triggers on: "quick take on X", ...').
    Extracting those quoted phrases gives a genuinely useful starting point
    instead of the previous placeholder (just the skill's own name with
    dashes replaced by spaces) - Sati still reviews/edits before import,
    this just removes the "type them all from scratch" step for the common
    case. A trailing single-letter placeholder token (the "X" in "quick
    take on X") is stripped - CASE's substring matching has no wildcard
    concept, so "quick take on" still correctly matches "quick take on
    Tesla"; the literal "X" would not match anything real. Returns []
    (not a crash) when the description has no quoted phrases at all - the
    caller falls back to asking Sati to type triggers, or suggests manual/
    semantic mode instead."""
    phrases = []
    seen = set()
    for m in _QUOTED_PHRASE_RE.finditer(description or ""):
        phrase = (m.group(1) or m.group(2) or "").strip()
        phrase = re.sub(r"\s+[A-Z]$", "", phrase)
        phrase_lower = phrase.lower()
        if phrase and phrase_lower not in seen:
            seen.add(phrase_lower)
            phrases.append(phrase)
    return phrases[:12]


def delete_skill(name: str) -> str:
    for skill in list_skills():
        if skill["name"] == name:
            Path(skill["_path"]).unlink()
            return f"Deleted skill '{name}'."
    return f"ERROR: no skill named '{name}' found."


def _slugify(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name.strip().lower()).strip("-") or "skill"


def import_skill(name: str, description: str, triggers: list, body: str, mode: str = "keyword") -> str:
    """Write a new CASE skill file - used both for importing an external
    Claude-Code-format skill (Sati supplies triggers for keyword mode,
    since that format has none of its own) and for authoring a skill from
    scratch. Fails loudly rather than guessing if a required field is
    missing - a keyword-mode skill with no triggers would just silently
    never fire, worse than an upfront error. semantic/manual mode don't
    need triggers at all - see match_skills()'s own docstring for what
    each mode actually does at match time."""
    name = (name or "").strip()
    triggers = [t.strip() for t in (triggers or []) if t.strip()]
    mode = mode if mode in ("keyword", "semantic", "manual") else "keyword"
    if not name:
        return "ERROR: a skill needs a name."
    if mode == "keyword" and not triggers:
        return "ERROR: a keyword-mode skill needs at least one trigger word/phrase, or it will never fire. Use semantic or manual mode instead if you don't want to write triggers."
    SKILLS_DIR.mkdir(exist_ok=True)
    target = SKILLS_DIR / f"{_slugify(name)}.md"
    triggers_str = ", ".join(triggers)
    content = (
        f"---\nname: {name}\ndescription: {description or ''}\n"
        f"triggers: [{triggers_str}]\nmode: {mode}\n---\n\n{(body or '').strip()}\n"
    )
    target.write_text(content, encoding="utf-8")
    return f"Saved skill '{name}' to {target}."


def import_skill_file(path: str) -> str:
    """Import a raw .md file already in CASE's OWN format (frontmatter with
    name/description/triggers) - e.g. one authored elsewhere and picked via
    a native file dialog. Copies it in only after confirming it actually
    parses, so a malformed file gives a clear error instead of silently
    sitting in skills/ doing nothing."""
    src = Path(path)
    if not src.exists() or src.is_dir():
        return f"ERROR: '{path}' isn't a file."
    parsed = _parse_skill_file(src)
    if parsed is None:
        return (
            f"ERROR: '{src.name}' doesn't look like a CASE skill file - needs "
            f"YAML frontmatter with at least 'name' and a non-empty 'triggers' "
            f"list. (A Claude Code-format SKILL.md won't parse directly here - "
            f"use 'scan for skills on this device' to import one of those instead.)"
        )
    SKILLS_DIR.mkdir(exist_ok=True)
    target = SKILLS_DIR / f"{_slugify(parsed['name'])}.md"
    target.write_bytes(src.read_bytes())
    return f"Imported skill '{parsed['name']}' to {target}."


def _parse_claude_skill_frontmatter(path: Path) -> dict | None:
    """Claude-Code-style SKILL.md: YAML frontmatter with name/description,
    no 'triggers' field - a different, incompatible format from CASE's own
    (see module docstring), so parsed separately rather than reusing
    _parse_skill_file (which requires triggers to accept a file at all)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    frontmatter_text, body = m.groups()
    raw_meta = _parse_yaml_frontmatter_lines(frontmatter_text)
    meta = {k: v.strip("\"'") for k, v in raw_meta.items() if k in ("name", "description")}
    if "name" not in meta:
        return None
    meta.setdefault("description", "")
    meta["body"] = body.strip()
    meta["source_path"] = str(path)
    return meta


def find_importable_skills() -> list:
    """Claude-Code-format skills (SKILL.md under a named folder) found in
    CLAUDE_SKILLS_SEARCH_PATHS that aren't already present in CASE's own
    skills/ (by name) - candidates for the Settings > Skills 'scan for
    skills on this device' action. Returns [{name, description, body,
    source_path}]; Sati supplies triggers at import time via import_skill."""
    existing_names = {s["name"] for s in list_skills()}
    candidates = []
    for base in CLAUDE_SKILLS_SEARCH_PATHS:
        if not base.exists():
            continue
        for skill_md in sorted(base.glob("*/SKILL.md")):
            meta = _parse_claude_skill_frontmatter(skill_md)
            if meta and meta["name"] not in existing_names:
                candidates.append(meta)
    return candidates
