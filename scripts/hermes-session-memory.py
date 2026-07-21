#!/usr/bin/env python3
"""
Hermes on_session_end hook — extract last user messages from session and
append a short note to a session-notes file. No LLM call (zero Cointh token spend).

Hook payload (from Hermes):
  {
    "event": "on_session_end",
    "session_id": "...",
    "task_id": "...",
    "turn_id": "...",
    "completed": true/false,
    "model": "glm-5.2",
    "platform": "telegram"
  }

Strategy: read the most recent request_dump file matching this session_id,
extract last user messages, append to memories/session-notes.md with timestamp.

Path resolution (root cause fix, 2026-07-21):
  - HERMES_HOME env = root hermes dir (e.g. AppData/Local/hermes), NOT the
    profile dir. Dump files and memory files live under profiles/<name>/.
  - We look up the active profile via HERMES_PROFILE env (Hermes sets it)
    and fall back to "glm" (only profile configured on this host).
  - SESSIONS_DIR = PROFILE_DIR/sessions, MEMORY_FILE = PROFILE_DIR/memories/
  - We deliberately write to session-notes.md (a separate file) instead of
    memories/MEMORY.md to avoid clobbering Hermes's own auto-managed
    canonical memory store.

Falls back to no-op silently if anything is missing — hooks must never
raise (would block session rotation).
"""
import json
import os
import sys
import glob
from pathlib import Path
from datetime import datetime

# --- Path resolution (profile-aware) ---
_HERMES_ROOT = Path(os.environ.get("HERMES_HOME", "")).expanduser()
if not str(_HERMES_ROOT) or _HERMES_ROOT == Path("."):
    # Fallback for testing / when env not inherited (cron, hooks without env)
    _HERMES_ROOT = Path(r"C:\Users\Bew\AppData\Local\hermes")

_PROFILE_NAME = os.environ.get("HERMES_PROFILE", "") or "glm"

# Profile dir may BE the hermes root (legacy single-profile) or a subdir.
# Prefer profiles/<name> when it exists; otherwise fall back to root.
_CANDIDATE = _HERMES_ROOT / "profiles" / _PROFILE_NAME
HERMES_PROFILE_DIR = _CANDIDATE if _CANDIDATE.exists() else _HERMES_ROOT

SESSIONS_DIR = HERMES_PROFILE_DIR / "sessions"
MEMORIES_DIR = HERMES_PROFILE_DIR / "memories"
# Separate file so we never collide with Hermes's auto-managed MEMORY.md.
MEMORY_FILE = MEMORIES_DIR / "session-notes.md"

MAX_USER_MSGS = 5
MAX_MSG_LEN = 200  # truncate long messages
MIN_MSG_LEN = 2    # skip "hi" / single chars but keep short Thai phrases


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # never fail the hook

    session_id = payload.get("session_id", "")
    platform = payload.get("platform", "")
    if not session_id:
        return 0

    # Find request_dump files for this session, sorted oldest→newest
    pattern = str(SESSIONS_DIR / f"request_dump_{session_id}_*.json")
    dumps = sorted(glob.glob(pattern))
    if not dumps:
        return 0  # no transcripts available — nothing to extract

    # Walk newest→oldest, collect up to MAX_USER_MSGS unique user messages
    user_msgs: list[str] = []
    seen = set()
    for dump_file in reversed(dumps[-20:]):  # cap at last 20 dumps
        try:
            with open(dump_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            body = d.get("request", {}).get("body", {})
            if isinstance(body, str):
                body = json.loads(body)
            msgs = body.get("messages", []) if isinstance(body, dict) else []
            for m in reversed(msgs):
                if m.get("role") != "user":
                    continue
                content = m.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") in (None, "text")
                    )
                content = (content or "").strip()
                # Collapse whitespace + newlines for storage readability
                content = " ".join(content.split())
                # Skip empty, too-short, tool-result, or dedup
                if len(content) < MIN_MSG_LEN or content in seen:
                    continue
                # Skip pure JSON / tool-call echoes
                if content.startswith("{") and content.endswith("}"):
                    continue
                # Hermes injects a vision transcript (Gemini image description)
                # into user messages that contain images. Strip the entire
                # transcript block — keep only the real user text that follows
                # after the closing marker `[If you need a closer look ... ~]`.
                vision_end = content.find("use vision_analyze with image_url:")
                if vision_end != -1:
                    # Find end of the injected hint line (marked by ~])
                    line_end = content.find("]", vision_end)
                    if line_end != -1:
                        content = content[line_end + 1:].strip()
                # Also strip a leading "[The user sent an image" wrapper in
                # case the marker above is missing.
                if content.startswith("[The user sent an image"):
                    # try to find the end of the description block "]"
                    close = content.find("]")
                    if close != -1:
                        content = content[close + 1:].strip()
                if len(content) < MIN_MSG_LEN:
                    continue
                seen.add(content)
                user_msgs.append(content[:MAX_MSG_LEN])
                if len(user_msgs) >= MAX_USER_MSGS:
                    break
        except Exception:
            continue
        if len(user_msgs) >= MAX_USER_MSGS:
            break

    if not user_msgs:
        return 0

    # Build entry (reverse to chronological order)
    user_msgs.reverse()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"\n## Session {timestamp} ({platform}, {session_id[:19]})\n"]
    for msg in user_msgs:
        lines.append(f"- {msg}\n")

    # Ensure memories/ dir exists (it should, but be defensive)
    MEMORIES_DIR.mkdir(parents=True, exist_ok=True)

    # Append to session-notes.md (create if missing, with header). This file
    # is separate from Hermes's auto-managed memories/MEMORY.md so the two
    # never collide.
    if not MEMORY_FILE.exists():
        MEMORY_FILE.write_text(
            "# Hermes session notes\n\n"
            "Auto-extracted last user messages per ended session.\n"
            "Prune old entries manually when this file grows too large.\n",
            encoding="utf-8",
        )
    with open(MEMORY_FILE, "a", encoding="utf-8") as f:
        f.writelines(lines)

    return 0


if __name__ == "__main__":
    sys.exit(main())
