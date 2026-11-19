"""
Memory flush agent - extracts important knowledge from conversation context.

Spawned by session-end.py or pre-compact.py as a background process. Reads
pre-extracted conversation context from a .md file, uses the Claude Agent SDK
to decide what's worth saving, and appends the result to today's daily log.

Usage:
    uv run python flush.py <context_file.md> <session_id> [end_offset]

`end_offset` is the transcript byte offset this context ends at. It is saved
only after a successful flush, so a failed flush is retried by the next hook.
"""

from __future__ import annotations

# Recursion prevention: set this BEFORE any imports that might trigger Claude
import os
os.environ["CLAUDE_INVOKED_BY"] = "memory_flush"

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import (
    AGENT_EXTRA_ARGS,
    AGENT_SETTING_SOURCES,
    COMPILE_LOCK_FILE,
    COMPILE_LOCK_STALE_SECONDS,
    FALLBACK_MODEL,
    FLUSH_CHUNK_CHARS,
    FLUSH_MODEL,
)
from transcript import OFFSET_PREFIX, split_into_chunks, write_offset
from utils import (  # noqa: F401  (re-exported for tests and callers)
    acquire_compile_lock,
    acquire_session_lock,
    refresh_search_index,
    release_compile_lock,
)

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "daily"
SCRIPTS_DIR = ROOT / "scripts"
LOG_FILE = SCRIPTS_DIR / "flush.log"

# Set up file-based logging so we can verify the background process ran.
# The parent process sends stdout/stderr to DEVNULL (to avoid the inherited
# file handle bug on Windows), so this is our only observability channel.
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def append_to_daily_log(content: str, section: str = "Session") -> None:
    """Append content to today's daily log."""
    today = datetime.now(timezone.utc).astimezone()
    log_path = DAILY_DIR / f"{today.strftime('%Y-%m-%d')}.md"

    if not log_path.exists():
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"# Daily Log: {today.strftime('%Y-%m-%d')}\n\n## Sessions\n\n## Memory Maintenance\n\n",
            encoding="utf-8",
        )

    time_str = today.strftime("%H:%M")
    entry = f"### {section} ({time_str})\n\n{content}\n\n"

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)


ENTRY_FORMAT = """Format your response as a structured daily log entry with these sections:

**Context:** [One line about what the user was working on]

**Key Exchanges:**
- [Important Q&A or discussions]

**Decisions Made:**
- [Any decisions with rationale]

**Lessons Learned:**
- [Gotchas, patterns, or insights discovered]

**Action Items:**
- [Follow-ups or TODOs mentioned]

Skip anything that is:
- Routine tool calls or file reads
- Content that's trivial or obvious
- Trivial back-and-forth or clarification exchanges

Only include sections that have actual content. If nothing is worth saving,
respond with exactly: FLUSH_OK"""


def build_single_prompt(context: str) -> str:
    return f"""Review the conversation below and respond with a concise summary
of important items that should be preserved in the daily log.
Lines like `[tool: Name — detail]` mark tool calls the assistant made.
Do NOT use any tools — just return plain text.

{ENTRY_FORMAT}

## Conversation

{context}"""


def build_chunk_prompt(chunk: str, index: int, total: int) -> str:
    return f"""This is part {index} of {total} of one long conversation. Extract working
notes that a later step will merge into a daily log entry: what the user was
doing, decisions and their rationale, lessons and gotchas, concrete facts
(names, numbers, paths, IDs), and follow-ups. Keep specifics; drop routine
tool calls and small talk. Lines like `[tool: Name — detail]` mark tool calls.
Do NOT use any tools — just return plain text. If this part has nothing worth
keeping, respond with exactly: NOTHING

## Conversation part {index}/{total}

{chunk}"""


def build_merge_prompt(notes: list[str]) -> str:
    joined = "\n\n".join(f"### Notes from part {i}\n\n{n}" for i, n in enumerate(notes, 1))
    return f"""Below are working notes extracted from consecutive parts of one
conversation. Merge them into a single daily log entry. Later parts win where
they revise earlier ones (a decision changed, a fix replaced). Do not invent
anything that is not in the notes.
Do NOT use any tools — just return plain text.

{ENTRY_FORMAT}

## Notes

{joined}"""


class FlushError(Exception):
    pass


async def ask(prompt: str) -> str:
    """One isolated, tool-less model call. Raises FlushError on failure."""
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    response = ""
    stderr_lines: list[str] = []
    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=str(ROOT),
                allowed_tools=[],
                max_turns=2,
                model=FLUSH_MODEL,
                fallback_model=FALLBACK_MODEL,
                setting_sources=AGENT_SETTING_SOURCES,
                extra_args=AGENT_EXTRA_ARGS,
                stderr=lambda line: stderr_lines.append(line.rstrip()),
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response += block.text
    except Exception as e:
        import traceback
        logging.error("Agent SDK error: %s\n%s", e, traceback.format_exc())
        if stderr_lines:
            logging.error("CLI stderr (last %d lines):\n%s", min(len(stderr_lines), 20),
                          "\n".join(stderr_lines[-20:]))
        # The CLI reports API failures as assistant text, so if we got any
        # text back it is the most useful thing to record.
        detail = response.strip() or (stderr_lines[-1] if stderr_lines else "")
        message = f"{type(e).__name__}: {e}"
        if detail:
            message += f"\nDetail: {detail[:500]}"
        raise FlushError(message) from e
    if not response.strip():
        raise FlushError("empty response from the model")
    return response


async def run_flush(context: str, chunk_chars: int = FLUSH_CHUNK_CHARS, ask_fn=ask) -> str:
    """Summarize the whole conversation: one call if it fits, map + merge if not."""
    chunks = split_into_chunks(context, chunk_chars)
    try:
        if len(chunks) <= 1:
            return await ask_fn(build_single_prompt(chunks[0] if chunks else context))

        logging.info("Long session: %d chunks", len(chunks))
        notes = []
        for i, chunk in enumerate(chunks, 1):
            note = (await ask_fn(build_chunk_prompt(chunk, i, len(chunks)))).strip()
            if note and note != "NOTHING":
                notes.append(note)
        if not notes:
            return "FLUSH_OK"
        if len(notes) == 1 and len(chunks) == 1:
            return notes[0]
        return await ask_fn(build_merge_prompt(notes))
    except FlushError as e:
        return f"FLUSH_ERROR: {e}"


COMPILE_AFTER_HOUR = 18  # 6 PM local time


def cleanup_stale_context_files(max_age_days: int = 7) -> None:
    """Remove context files and session locks left behind by flush runs that
    died before their own cleanup (crash, kill, power loss)."""
    cutoff = time.time() - max_age_days * 86400
    # Offsets live longer: a session resumed after a week must not be
    # re-summarized from its first turn.
    offset_cutoff = time.time() - 60 * 86400
    patterns = ("session-flush-*.md", "flush-context-*.md", "session-lock-*", f"{OFFSET_PREFIX}*")
    for pattern in patterns:
        for stale in SCRIPTS_DIR.glob(pattern):
            try:
                limit = offset_cutoff if pattern.startswith(OFFSET_PREFIX) else cutoff
                if stale.stat().st_mtime < limit:
                    stale.unlink()
                    logging.info("Removed stale file: %s", stale.name)
            except OSError:
                pass


def maybe_trigger_compilation() -> None:
    """If it's past the compile hour and today's log hasn't been compiled, run compile.py."""
    import subprocess as _sp

    now = datetime.now(timezone.utc).astimezone()
    if now.hour < COMPILE_AFTER_HOUR:
        return

    # Check if today's log has already been compiled
    today_log = f"{now.strftime('%Y-%m-%d')}.md"
    compile_state_file = SCRIPTS_DIR / "state.json"
    if compile_state_file.exists():
        try:
            compile_state = json.loads(compile_state_file.read_text(encoding="utf-8"))
            ingested = compile_state.get("ingested", {})
            if today_log in ingested:
                # Already compiled today - check if the log has changed since
                from hashlib import sha256
                log_path = DAILY_DIR / today_log
                if log_path.exists():
                    current_hash = sha256(log_path.read_bytes()).hexdigest()[:16]
                    if ingested[today_log].get("hash") == current_hash:
                        return  # log unchanged since last compile
        except (json.JSONDecodeError, OSError):
            pass

    compile_script = SCRIPTS_DIR / "compile.py"
    if not compile_script.exists():
        return

    # compile.py takes this lock itself; checking here just avoids spawning a
    # process that would exit immediately.
    try:
        if COMPILE_LOCK_FILE.exists() and \
                time.time() - COMPILE_LOCK_FILE.stat().st_mtime < COMPILE_LOCK_STALE_SECONDS:
            logging.info("Compile already running (lock present), not spawning another")
            return
    except OSError:
        pass

    logging.info("End-of-day compilation triggered (after %d:00)", COMPILE_AFTER_HOUR)

    cmd = ["uv", "run", "--directory", str(ROOT), "python", str(compile_script)]

    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _sp.CREATE_NEW_PROCESS_GROUP | _sp.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True

    try:
        log_handle = open(str(SCRIPTS_DIR / "compile.log"), "a")
        _sp.Popen(cmd, stdout=log_handle, stderr=_sp.STDOUT, cwd=str(ROOT), **kwargs)
    except Exception as e:
        logging.error("Failed to spawn compile.py: %s", e)


def main():
    if len(sys.argv) < 3:
        logging.error("Usage: %s <context_file.md> <session_id>", sys.argv[0])
        sys.exit(1)

    context_file = Path(sys.argv[1])
    session_id = sys.argv[2]
    end_offset = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else None

    logging.info("flush.py started for session %s, context: %s", session_id, context_file)

    if not context_file.exists():
        logging.error("Context file not found: %s", context_file)
        return

    # Deduplication: the same hook registered in two settings files fires twice
    # in the same second. Only the process that wins the atomic lock proceeds.
    if not acquire_session_lock(session_id, SCRIPTS_DIR):
        logging.info("Skipping duplicate flush for session %s", session_id)
        context_file.unlink(missing_ok=True)
        return

    # Read pre-extracted context
    context = context_file.read_text(encoding="utf-8").strip()
    if not context:
        logging.info("Context file is empty, skipping")
        context_file.unlink(missing_ok=True)
        return

    logging.info("Flushing session %s: %d chars (model=%s)", session_id, len(context), FLUSH_MODEL)

    # Run the LLM extraction
    response = asyncio.run(run_flush(context))

    # Append to daily log
    failed = response.startswith("FLUSH_ERROR")
    if not failed and end_offset is not None:
        write_offset(SCRIPTS_DIR, session_id, end_offset)

    if failed:
        logging.error("Result: %s", response)
        append_to_daily_log(response, "Memory Flush")
    elif response.strip() == "FLUSH_OK":
        logging.info("Result: FLUSH_OK")
        append_to_daily_log(
            "FLUSH_OK - Nothing worth saving from this session", "Memory Flush"
        )
    else:
        logging.info("Result: saved to daily log (%d chars)", len(response))
        append_to_daily_log(response, "Session")

    # Clean up context file
    context_file.unlink(missing_ok=True)

    # Sweep leftovers from flush runs that never reached their own cleanup
    cleanup_stale_context_files()

    # Make the new daily entry searchable (keyword index; embeddings follow the compile)
    refresh_search_index()

    # End-of-day auto-compilation: if it's past the compile hour and today's
    # log hasn't been compiled yet, trigger compile.py in the background.
    maybe_trigger_compilation()

    logging.info("Flush complete for session %s", session_id)


if __name__ == "__main__":
    main()
