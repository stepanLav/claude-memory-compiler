"""
SessionStart hook - tells the session what memory exists and how to reach it.

Claude Code caps a hook's additionalContext at 10,000 characters; anything
longer is written to a file and the model sees a 2,000-character preview. The
previous version injected ~36 KB of indexes, so sessions effectively received
eight lines of the wiki index and nothing else.

So this hook injects a short briefing instead of the indexes: where the
knowledge lives, how to search it (qmd), the newest article titles, and the
tail of the latest daily log. Details are pulled on demand.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT / "knowledge"
DAILY_DIR = ROOT / "daily"
INDEX_FILE = KNOWLEDGE_DIR / "index.md"
PROJECT_ROOT = ROOT.parent
WIKI_DIR = PROJECT_ROOT / "wiki"

HARD_LIMIT = 10_000          # Claude Code's cap per additionalContext string
MAX_CONTEXT_CHARS = 9_000    # stay clear of it
RECENT_ARTICLES_BUDGET = 2_600
DAILY_LOG_BUDGET = 4_500
MAX_LOG_LINES = 45
SUMMARY_MAX_CHARS = 110
LOOKBACK_DAYS = 4  # Monday still sees Friday's log

_ROW_RE = re.compile(r"^\|\s*\[\[([^\]]+)\]\]\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(\S+)\s*\|\s*$")

HOW_TO_SEARCH = """## Memory: what exists and how to reach it

The user keeps two knowledge bases in {project_root}. They are NOT loaded into this session - search them when the task could have history:

- **Engineering knowledge base** ({kb_count} articles compiled from past sessions: bugs, root causes, review findings, decisions, how earlier tasks were done). Collection `knowledge`.
- **Domain wiki** ({wiki_count} pages: concepts, products, people, analyses, meeting notes with action items). Collection `wiki`.
- **Daily session logs** (what was done and decided, day by day). Collection `daily`.

Search with the `qmd` MCP tools (`query`, then `get`) or the CLI: `qmd query "<question in English>" -n 8`, `qmd search "<exact term, ticket id, PR number>"`, `qmd get <path>`. Articles are written in English - query in English.

Search before: debugging something that may have happened before, reviewing or changing an area worked on earlier, answering "how did we / why did we", continuing a task from a previous session. Skip it for self-contained tasks."""


def get_recent_log(daily_dir: Path = DAILY_DIR, today: datetime | None = None,
                   lookback_days: int = LOOKBACK_DAYS) -> str:
    """Read the most recent daily log within the last `lookback_days` days."""
    today = today or datetime.now(timezone.utc).astimezone()
    for offset in range(lookback_days):
        date = today - timedelta(days=offset)
        log_path = daily_dir / f"{date.strftime('%Y-%m-%d')}.md"
        if log_path.exists():
            return f"({log_path.name})\n" + log_path.read_text(encoding="utf-8")
    return "(no recent daily log)"


def parse_kb_rows(index_md: str) -> list[tuple[str, str, str]]:
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
    return rows


def compact_kb_index(index_md: str, budget: int = RECENT_ARTICLES_BUDGET) -> str:
    """Newest articles as `- [[slug]] — summary (date)` lines, cut to `budget`."""
    rows = parse_kb_rows(index_md)
    header = f"{len(rows)} articles total; newest first (search the rest with qmd).\n"
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


def _display_path(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def count_wiki_pages(wiki_dir: Path = WIKI_DIR) -> int:
    try:
        return sum(1 for p in wiki_dir.rglob("*.md") if p.name not in ("index.md", "log.md"))
    except OSError:
        return 0


def build_context(
    *,
    kb_index: str | None,
    recent_log: str,
    wiki_count: int = 0,
    today: datetime | None = None,
) -> str:
    """Assemble the briefing. Always shorter than Claude Code's 10k cap."""
    today = today or datetime.now(timezone.utc).astimezone()
    kb_count = len(parse_kb_rows(kb_index)) if kb_index else 0

    parts = [
        f"## Today\n{today.strftime('%A, %B %d, %Y')}",
        HOW_TO_SEARCH.format(kb_count=kb_count, wiki_count=wiki_count, project_root=_display_path(PROJECT_ROOT)),
    ]
    if kb_index:
        parts.append("## Newest knowledge base articles\n\n" + compact_kb_index(kb_index))
    parts.append(
        "## Latest daily log (tail)\n\n" + _tail_lines(recent_log, MAX_LOG_LINES, DAILY_LOG_BUDGET)
    )

    context = "\n\n---\n\n".join(parts)
    if len(context) > MAX_CONTEXT_CHARS:  # defensive; the budgets above keep it under
        context = context[:MAX_CONTEXT_CHARS].rsplit("\n", 1)[0] + "\n\n...(truncated)"
    return context


def main():
    # Flush/compile agents run Claude Code themselves; they need none of this.
    if os.environ.get("CLAUDE_INVOKED_BY"):
        sys.exit(0)

    kb_index = INDEX_FILE.read_text(encoding="utf-8") if INDEX_FILE.exists() else None
    context = build_context(
        kb_index=kb_index,
        recent_log=get_recent_log(),
        wiki_count=count_wiki_pages(),
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))


if __name__ == "__main__":
    main()
