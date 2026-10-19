"""
SessionStart hook - injects knowledge base context into every conversation.

This is the "context injection" layer. When Claude Code starts a session,
this hook reads both knowledge bases (wiki + compiled knowledge) and the
recent daily log, then injects them as additional context so Claude always
"remembers" what it has learned.

Each section gets its own character budget so a large wiki index can never
crowd out the knowledge base index or the daily log (which is what happened
when a single 20k cap was applied to the concatenation).
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Paths relative to project root (claude-memory-compiler/)
ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT / "knowledge"
DAILY_DIR = ROOT / "daily"
INDEX_FILE = KNOWLEDGE_DIR / "index.md"

# Wiki lives one level up in the parent project (claude-power/)
PROJECT_ROOT = ROOT.parent
WIKI_INDEX_FILE = PROJECT_ROOT / "wiki" / "index.md"

# Per-section budgets (characters). ~4 chars per token.
WIKI_INDEX_BUDGET = 20_000
KB_INDEX_BUDGET = 12_000
DAILY_LOG_BUDGET = 5_000
MAX_CONTEXT_CHARS = WIKI_INDEX_BUDGET + KB_INDEX_BUDGET + DAILY_LOG_BUDGET + 2_000
MAX_LOG_LINES = 40
SUMMARY_MAX_CHARS = 110

_ROW_RE = re.compile(r"^\|\s*\[\[([^\]]+)\]\]\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(\S+)\s*\|\s*$")


LOOKBACK_DAYS = 4  # Monday still sees Friday's log


def get_recent_log(daily_dir: Path = DAILY_DIR, today: datetime | None = None,
                   lookback_days: int = LOOKBACK_DAYS) -> str:
    """Read the most recent daily log within the last `lookback_days` days."""
    today = today or datetime.now(timezone.utc).astimezone()

    for offset in range(lookback_days):
        date = today - timedelta(days=offset)
        log_path = daily_dir / f"{date.strftime('%Y-%m-%d')}.md"
        if log_path.exists():
            return log_path.read_text(encoding="utf-8")

    return "(no recent daily log)"


def compact_kb_index(index_md: str, budget: int = KB_INDEX_BUDGET) -> str:
    """Turn the padded index table into `- [[slug]] — summary (date)` lines,
    newest first, cut to `budget` characters.

    The full table is ~240 KB; a compact, recency-ordered excerpt is what fits
    into a session. The header tells the model the full index exists on disk.
    """
    rows = []
    for line in index_md.splitlines():
        m = _ROW_RE.match(line)
        if not m:
            continue
        slug, summary, _sources, updated = m.groups()
        if slug.lower() == "article":
            continue
        rows.append((slug, summary, updated))

    rows.sort(key=lambda r: r[2], reverse=True)

    header = (
        f"{len(rows)} articles total; newest first, full table in knowledge/index.md "
        "(Grep it or Read an article for details).\n"
    )
    out = [header]
    size = len(header)
    for slug, summary, updated in rows:
        if len(summary) > SUMMARY_MAX_CHARS:
            summary = summary[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"
        entry = f"- [[{slug}]] — {summary} ({updated})"
        if size + len(entry) + 1 > budget:
            break
        out.append(entry)
        size += len(entry) + 1
    return "\n".join(out)


def _truncate_head(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    return text[:budget].rsplit("\n", 1)[0] + "\n\n...(truncated)"


def _tail_lines(text: str, max_lines: int, budget: int) -> str:
    lines = text.splitlines()
    recent = lines[-max_lines:] if len(lines) > max_lines else lines
    out = "\n".join(recent)
    if len(out) > budget:
        out = out[-budget:]
        boundary = out.find("\n")
        if boundary > 0:
            out = out[boundary + 1:]
    return out


def build_context(
    *,
    wiki_index: str | None,
    kb_index: str | None,
    recent_log: str,
    today: datetime | None = None,
) -> str:
    """Assemble the context to inject into the conversation."""
    today = today or datetime.now(timezone.utc).astimezone()
    parts = [f"## Today\n{today.strftime('%A, %B %d, %Y')}"]

    # Wiki index (domain knowledge — what things are)
    if wiki_index:
        parts.append(
            "## Wiki Index (Domain Knowledge)\n\n" + _truncate_head(wiki_index, WIKI_INDEX_BUDGET)
        )

    # Knowledge base index (engineering lessons — what was learned)
    if kb_index:
        parts.append(
            "## Knowledge Base Index (Engineering Lessons)\n\n"
            + compact_kb_index(kb_index, KB_INDEX_BUDGET)
        )
    else:
        parts.append("## Knowledge Base Index\n\n(empty - no articles compiled yet)")

    # Recent daily log
    parts.append(
        "## Recent Daily Log\n\n" + _tail_lines(recent_log, MAX_LOG_LINES, DAILY_LOG_BUDGET)
    )

    context = "\n\n---\n\n".join(parts)
    if len(context) > MAX_CONTEXT_CHARS:  # defensive; budgets above make this unreachable
        context = context[:MAX_CONTEXT_CHARS] + "\n\n...(truncated)"
    return context


def main():
    wiki_index = WIKI_INDEX_FILE.read_text(encoding="utf-8") if WIKI_INDEX_FILE.exists() else None
    kb_index = INDEX_FILE.read_text(encoding="utf-8") if INDEX_FILE.exists() else None

    context = build_context(
        wiki_index=wiki_index,
        kb_index=kb_index,
        recent_log=get_recent_log(),
    )

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }

    print(json.dumps(output))


if __name__ == "__main__":
    main()
