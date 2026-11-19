"""Shared body of the SessionEnd and PreCompact hooks.

Reads the hook input, extracts every conversation turn the previous flush has
not seen yet, and spawns flush.py in the background. No API calls here - only
local file I/O, so the hook stays well inside its timeout.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from transcript import extract_turns, join_turns, read_offset  # noqa: E402


def run_capture(*, event: str, log_tag: str, file_prefix: str, min_turns: int) -> None:
    # Recursion guard: flush.py runs Claude Code through the Agent SDK, which
    # would fire this hook again.
    if os.environ.get("CLAUDE_INVOKED_BY"):
        return

    logging.basicConfig(
        filename=str(SCRIPTS_DIR / "flush.log"),
        level=logging.INFO,
        format=f"%(asctime)s %(levelname)s [{log_tag}] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        raw_input = sys.stdin.read()
        try:
            hook_input: dict = json.loads(raw_input)
        except json.JSONDecodeError:
            # Claude Code on Windows may pass paths with unescaped backslashes
            hook_input = json.loads(re.sub(r'(?<!\\)\\(?!["\\])', r"\\\\", raw_input))
    except (json.JSONDecodeError, ValueError, EOFError) as e:
        logging.error("Failed to parse stdin: %s", e)
        return

    session_id = hook_input.get("session_id", "unknown")
    transcript_path_str = hook_input.get("transcript_path", "")
    logging.info("%s fired: session=%s reason=%s", event, session_id, hook_input.get("reason", "-"))

    if not transcript_path_str or not isinstance(transcript_path_str, str):
        logging.info("SKIP: no transcript path")
        return
    transcript_path = Path(transcript_path_str)
    if not transcript_path.exists():
        logging.info("SKIP: transcript missing: %s", transcript_path_str)
        return

    start_offset = read_offset(SCRIPTS_DIR, session_id)
    try:
        turns, end_offset = extract_turns(transcript_path, start_offset)
    except Exception as e:
        logging.error("Context extraction failed: %s", e)
        return

    if len(turns) < min_turns:
        logging.info("SKIP: only %d new turns since offset %d (min %d)", len(turns), start_offset, min_turns)
        return

    context = join_turns(turns)
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    context_file = SCRIPTS_DIR / f"{file_prefix}-{session_id}-{timestamp}.md"
    context_file.write_text(context, encoding="utf-8")

    cmd = [
        "uv", "run", "--directory", str(ROOT), "python",
        str(SCRIPTS_DIR / "flush.py"), str(context_file), session_id, str(end_offset),
    ]
    # On Windows, CREATE_NO_WINDOW avoids a console flash. Do NOT use
    # DETACHED_PROCESS - it breaks the Agent SDK's subprocess I/O.
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=creation_flags)
        logging.info("Spawned flush.py for session %s (%d turns, %d chars, bytes %d-%d)",
                     session_id, len(turns), len(context), start_offset, end_offset)
    except Exception as e:
        logging.error("Failed to spawn flush.py: %s", e)
