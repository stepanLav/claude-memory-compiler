"""
Compile daily conversation logs into structured knowledge articles.

This is the "LLM compiler" - it reads daily logs (source code) and produces
organized knowledge articles (the executable).

Usage:
    uv run python compile.py                    # compile new/changed logs only
    uv run python compile.py --all              # force recompile everything
    uv run python compile.py --file daily/2026-04-01.md  # compile a specific log
    uv run python compile.py --dry-run          # show what would be compiled
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from config import (
    AGENTS_FILE,
    AUTO_SYNC_DATA_REPO,
    COMPILE_LOCK_FILE,
    COMPILE_MODEL,
    CONCEPTS_DIR,
    CONNECTIONS_DIR,
    DAILY_DIR,
    FALLBACK_MODEL,
    KNOWLEDGE_DIR,
    now_iso,
)
from utils import (
    acquire_compile_lock,
    compact_table_whitespace,
    file_hash,
    list_raw_files,
    list_wiki_articles,
    load_state,
    read_wiki_index,
    release_compile_lock,
    save_state,
)

# ── Paths for the LLM to use ──────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent


def build_prompt(
    *,
    log_name: str,
    log_content: str,
    schema: str,
    wiki_index: str,
    domain_wiki_index: str,
    existing_articles: dict[str, str],
    timestamp: str,
) -> str:
    """Assemble the compile prompt.

    Existing articles are listed by path only. The agent has Read/Glob/Grep
    and reads the handful it needs. Inlining every article body (as this
    script used to do) grew the prompt linearly with the knowledge base and
    reached ~880k tokens per call at 433 articles, where it stopped working.
    """
    index_compact = compact_table_whitespace(wiki_index)
    domain_compact = compact_table_whitespace(domain_wiki_index) if domain_wiki_index else ""

    if existing_articles:
        article_list = "\n".join(f"- {rel}" for rel in sorted(existing_articles))
        existing_section = (
            f"{len(existing_articles)} articles exist under `{KNOWLEDGE_DIR}`. Their bodies are "
            "NOT included here. Before you update an article, or decide whether a concept "
            "already has one, Read it from disk (or Grep `knowledge/` for the topic).\n\n"
            f"{article_list}"
        )
    else:
        existing_section = "(No existing articles yet)"

    return f"""You are a knowledge compiler. Your job is to read a daily conversation log
and extract knowledge into structured wiki articles.

## Schema (AGENTS.md)

{schema}

## Current Wiki Index

{index_compact}

## Existing Wiki Articles

{existing_section}

## Domain Wiki Index (domain wiki at <project root>/wiki/)

{domain_compact if domain_compact else "(No domain wiki found)"}

Use this index to add `## Wiki Context` sections to knowledge articles when a matching
domain page exists. Links are relative to the article file: from `knowledge/concepts/<slug>.md`
or `knowledge/connections/<slug>.md` that is `../../../wiki/...`, e.g. `../../../wiki/concepts/xcm.md`.

## Daily Log to Compile

**File:** {log_name}

{log_content}

## Your Task

Read the daily log above and compile it into wiki articles following the schema exactly.

### Rules:

1. **Extract key concepts** - Identify 3-7 distinct concepts worth their own article
2. **Create concept articles** in `knowledge/concepts/` - One .md file per concept
   - Use the exact article format from AGENTS.md (YAML frontmatter + sections)
   - Include `sources:` in frontmatter pointing to the daily log file
   - Use `[[concepts/slug]]` wikilinks to link to related concepts
   - Write in encyclopedia style - neutral, comprehensive
3. **Create connection articles** in `knowledge/connections/` if this log reveals non-obvious
   relationships between 2+ existing concepts
4. **Update existing articles** if this log adds new information to concepts already in the wiki
   - Read the existing article first, add the new information, add the source to frontmatter
   - Check the index summaries above for likely matches before creating a near-duplicate
5. **Update knowledge/index.md** - Add new entries to the table
   - Each entry: `| [[path/slug]] | One-line summary | source-file | {timestamp[:10]} |`
6. **Cross-link with domain wiki** - If an article covers a topic matching a page in the
   Domain Wiki Index above, add a `## Wiki Context` section at the bottom with a relative
   link (relative to the article file), e.g.: `- [XCM](../../../wiki/concepts/xcm.md) — domain overview of Cross-Consensus Messaging`
7. **Append to knowledge/log.md** - Add a timestamped entry:
   ```
   ## [{timestamp}] compile | {log_name}
   - Source: daily/{log_name}
   - Articles created: [[concepts/x]], [[concepts/y]]
   - Articles updated: [[concepts/z]] (if any)
   ```
   If the log contains only FLUSH_OK / FLUSH_ERROR entries and no session content, write
   the log entry with "(none)" for created/updated and stop - do not invent articles.

### File paths:
- Write concept articles to: {CONCEPTS_DIR}
- Write connection articles to: {CONNECTIONS_DIR}
- Update index at: {KNOWLEDGE_DIR / 'index.md'}
- Append log at: {KNOWLEDGE_DIR / 'log.md'}

### Quality standards:
- Every article must have complete YAML frontmatter
- Every article must link to at least 2 other articles via [[wikilinks]]
- Key Points section should have 3-5 bullet points
- Details section should have 2+ paragraphs
- Related Concepts section should have 2+ entries
- Sources section should cite the daily log with specific claims extracted
"""


async def compile_daily_log(log_path: Path, state: dict) -> float:
    """Compile a single daily log into knowledge articles.

    Returns the API cost of the compilation.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    log_content = log_path.read_text(encoding="utf-8")
    schema = AGENTS_FILE.read_text(encoding="utf-8")
    wiki_index = read_wiki_index()

    # Read the sibling wiki index for cross-referencing
    parent_wiki_index_path = ROOT_DIR.parent / "wiki" / "index.md"
    domain_wiki_index = ""
    if parent_wiki_index_path.exists():
        domain_wiki_index = parent_wiki_index_path.read_text(encoding="utf-8")

    # Only the paths are needed in the prompt; the agent reads bodies on demand.
    existing = {
        str(article_path.relative_to(KNOWLEDGE_DIR)): ""
        for article_path in list_wiki_articles()
    }

    prompt = build_prompt(
        log_name=log_path.name,
        log_content=log_content,
        schema=schema,
        wiki_index=wiki_index,
        domain_wiki_index=domain_wiki_index,
        existing_articles=existing,
        timestamp=now_iso(),
    )
    print(f"  Prompt: {len(prompt):,} chars, {len(existing)} existing articles listed")

    cost = 0.0
    stderr_lines: list[str] = []

    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=str(ROOT_DIR),
                system_prompt={"type": "preset", "preset": "claude_code"},
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
                permission_mode="acceptEdits",
                max_turns=30,
                model=COMPILE_MODEL,
                fallback_model=FALLBACK_MODEL,
                stderr=lambda line: stderr_lines.append(line.rstrip()),
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        pass  # compilation output - LLM writes files directly
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd or 0.0
                print(f"  Cost: ${cost:.4f}")
                if message.subtype != "success":
                    print(f"  Result: {message.subtype}")
    except Exception as e:
        import traceback

        print(f"  Error: {e}")
        if stderr_lines:
            print("  CLI stderr (tail):")
            for line in stderr_lines[-20:]:
                print(f"    {line}")
        print(traceback.format_exc())
        # The API cost was incurred even though compilation failed - record it
        # so total_cost stays honest, but don't mark the log as ingested
        # (a retry will pick it up again).
        if cost:
            state["total_cost"] = state.get("total_cost", 0.0) + cost
            save_state(state)
        return 0.0

    # Update state
    rel_path = log_path.name
    state.setdefault("ingested", {})[rel_path] = {
        "hash": file_hash(log_path),
        "compiled_at": now_iso(),
        "cost_usd": cost,
    }
    state["total_cost"] = state.get("total_cost", 0.0) + cost
    save_state(state)

    return cost


def main():
    parser = argparse.ArgumentParser(description="Compile daily logs into knowledge articles")
    parser.add_argument("--all", action="store_true", help="Force recompile all logs")
    parser.add_argument("--file", type=str, help="Compile a specific daily log file")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be compiled")
    args = parser.parse_args()

    state = load_state()

    # Determine which files to compile
    if args.file:
        target = Path(args.file)
        if not target.is_absolute():
            target = DAILY_DIR / target.name
        if not target.exists():
            # Try resolving relative to project root
            target = ROOT_DIR / args.file
        if not target.exists():
            print(f"Error: {args.file} not found")
            sys.exit(1)
        to_compile = [target]
    else:
        all_logs = list_raw_files()
        if args.all:
            to_compile = all_logs
        else:
            to_compile = []
            for log_path in all_logs:
                rel = log_path.name
                prev = state.get("ingested", {}).get(rel, {})
                if not prev or prev.get("hash") != file_hash(log_path):
                    to_compile.append(log_path)

    if not to_compile:
        print("Nothing to compile - all daily logs are up to date.")
        return

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Files to compile ({len(to_compile)}):")
    for f in to_compile:
        print(f"  - {f.name}")

    if args.dry_run:
        return

    if not acquire_compile_lock(COMPILE_LOCK_FILE):
        print(f"Another compile is running ({COMPILE_LOCK_FILE} exists). Exiting.")
        return

    try:
        # Compile each file sequentially
        total_cost = 0.0
        for i, log_path in enumerate(to_compile, 1):
            print(f"\n[{i}/{len(to_compile)}] Compiling {log_path.name}...")
            cost = asyncio.run(compile_daily_log(log_path, state))
            total_cost += cost
            print("  Done.")

        articles = list_wiki_articles()
        print(f"\nCompilation complete. Total cost: ${total_cost:.2f}")
        print(f"Knowledge base: {len(articles)} articles")
    finally:
        release_compile_lock(COMPILE_LOCK_FILE)

    # Free structural lint so broken links surface the day they appear
    # instead of accumulating silently until a manual lint run.
    try:
        import subprocess

        lint = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "lint.py"), "--structural-only"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        for line in lint.stdout.splitlines():
            if line.startswith(("Results:", "Report saved")):
                print(line)
    except Exception as e:
        print(f"Post-compile lint skipped: {e}")

    # Back up the knowledge data repo (daily/ + knowledge/) if it is a separate
    # git repo. Failures are reported, never raised.
    if AUTO_SYNC_DATA_REPO:
        from datasync import sync_data_repo

        print(f"Data repo sync: {sync_data_repo(KNOWLEDGE_DIR, forbidden_root=ROOT_DIR)}")


if __name__ == "__main__":
    main()
