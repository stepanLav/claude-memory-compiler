"""Turn a Claude Code JSONL transcript into markdown turns for the flush agent.

Shared by the SessionEnd and PreCompact hooks. Two properties matter:

* The whole conversation is extracted, not its tail. Sessions are typically
  100k+ characters; a 15k tail dropped most of the work from memory.
* Extraction is incremental. A session is flushed several times (PreCompact,
  then SessionEnd, then again after a resume), so each flush starts at the
  byte offset the previous successful flush reached.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

TURN_SEPARATOR = "\n<!--turn-->\n"
MAX_TURN_CHARS = 6_000      # one pasted log or giant answer must not crowd out the rest
MAX_TOOL_NOTE_CHARS = 140
OFFSET_PREFIX = "session-offset-"

# Harness-injected blocks that are not something the user or the model said.
_NOISE_RE = re.compile(
    r"<(system-reminder|task-notification|local-command-stdout|local-command-stderr|"
    r"local-command-caveat|command-message|persisted-output)\b[^>]*>.*?</\1>",
    re.DOTALL,
)


def offset_file(state_dir: Path, session_id: str) -> Path:
    return Path(state_dir) / f"{OFFSET_PREFIX}{session_id}"


def read_offset(state_dir: Path, session_id: str) -> int:
    try:
        return int(offset_file(state_dir, session_id).read_text().strip())
    except (OSError, ValueError):
        return 0


def write_offset(state_dir: Path, session_id: str, offset: int) -> None:
    offset_file(state_dir, session_id).write_text(str(offset))


def _clip(text: str, limit: int = MAX_TURN_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n…[{len(text) - limit} chars omitted]…\n{text[-tail:]}"


def _tool_note(block: dict) -> str:
    """One line saying which tool ran and on what - enough to know what was done."""
    name = block.get("name", "tool")
    inp = block.get("input") or {}
    detail = ""
    if isinstance(inp, dict):
        for key in ("description", "file_path", "skill", "pattern", "query", "url", "command", "prompt"):
            value = inp.get(key)
            if isinstance(value, str) and value.strip():
                detail = " ".join(value.split())
                break
    if len(detail) > MAX_TOOL_NOTE_CHARS:
        detail = detail[: MAX_TOOL_NOTE_CHARS - 1] + "…"
    return f"[tool: {name}{' — ' + detail if detail else ''}]"


def _render(entry: dict) -> str | None:
    if entry.get("type") not in ("user", "assistant"):
        return None
    # Subagent traffic, harness meta messages and compaction summaries are not
    # the conversation itself; the summary would also duplicate earlier turns.
    if entry.get("isSidechain") or entry.get("isMeta") or entry.get("isCompactSummary"):
        return None

    msg = entry.get("message")
    if not isinstance(msg, dict):
        return None
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return None

    content = msg.get("content", "")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use" and role == "assistant":
                    parts.append(_tool_note(block))

    text = _NOISE_RE.sub("", "\n".join(p for p in parts if p)).strip()
    if not text:
        return None
    label = "User" if role == "user" else "Assistant"
    return f"**{label}:** {_clip(text)}"


def extract_turns(transcript_path: Path, start_offset: int = 0) -> tuple[list[str], int]:
    """Return (turns, end_offset) for everything after `start_offset` bytes.

    Only complete lines are consumed, so a line still being written is picked
    up by the next flush instead of being lost.
    """
    turns: list[str] = []
    path = Path(transcript_path)
    size = path.stat().st_size
    if start_offset > size:  # transcript was replaced/rotated - start over
        start_offset = 0

    end_offset = start_offset
    with open(path, "rb") as f:
        f.seek(start_offset)
        for raw in f:
            if not raw.endswith(b"\n"):
                break
            end_offset += len(raw)
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            rendered = _render(entry)
            if rendered is None:
                continue
            # Collapse runs of tool-only assistant turns into the previous one.
            if (
                turns
                and rendered.startswith("**Assistant:** [tool:")
                and turns[-1].startswith("**Assistant:**")
                and "\n" not in rendered
            ):
                turns[-1] += "\n" + rendered.removeprefix("**Assistant:** ")
                continue
            turns.append(rendered)

    return turns, end_offset


def join_turns(turns: list[str]) -> str:
    return TURN_SEPARATOR.join(turns)


def split_into_chunks(context: str, chunk_chars: int) -> list[str]:
    """Pack whole turns into chunks of at most `chunk_chars` characters."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for turn in context.split(TURN_SEPARATOR):
        turn = turn.strip()
        if not turn:
            continue
        if current and size + len(turn) > chunk_chars:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(turn)
        size += len(turn) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks
