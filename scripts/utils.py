"""Shared utilities for the personal knowledge base."""

import hashlib
import json
import os
import re
import time
from pathlib import Path

from config import (
    COMPILE_LOCK_STALE_SECONDS,
    CONCEPTS_DIR,
    CONNECTIONS_DIR,
    DAILY_DIR,
    INDEX_FILE,
    KNOWLEDGE_DIR,
    LOG_FILE,
    QA_DIR,
    SESSION_LOCK_WINDOW_SECONDS,
    STATE_FILE,
)


# ── Locks ─────────────────────────────────────────────────────────────
#
# Both helpers rely on O_EXCL file creation, which is atomic on every
# platform we run on. A plain "read state, compare, write state" check
# cannot de-duplicate two processes that start in the same second.

def _try_create_exclusive(path: Path) -> bool:
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    return True


def _is_older_than(path: Path, seconds: float) -> bool:
    try:
        return time.time() - path.stat().st_mtime > seconds
    except OSError:
        return True  # vanished between checks - treat as expired


def acquire_session_lock(session_id: str, state_dir: Path,
                         window: float = SESSION_LOCK_WINDOW_SECONDS) -> bool:
    """Return True for the first flush of `session_id` inside `window` seconds.

    A second caller inside the window (e.g. the same hook registered twice)
    gets False and should skip. After the window expires the lock is reclaimed,
    so a PreCompact flush followed later by a SessionEnd flush still both run.
    """
    lock = Path(state_dir) / f"session-lock-{session_id}"
    if _try_create_exclusive(lock):
        return True
    if _is_older_than(lock, window):
        lock.unlink(missing_ok=True)
        return _try_create_exclusive(lock)
    return False


def acquire_compile_lock(lock_path: Path,
                         stale_seconds: float = COMPILE_LOCK_STALE_SECONDS) -> bool:
    """Exclusive lock for compile.py. Reclaims locks left by crashed runs."""
    lock = Path(lock_path)
    if _try_create_exclusive(lock):
        return True
    if _is_older_than(lock, stale_seconds):
        lock.unlink(missing_ok=True)
        return _try_create_exclusive(lock)
    return False


def release_compile_lock(lock_path: Path) -> None:
    Path(lock_path).unlink(missing_ok=True)


def compact_table_whitespace(markdown_table: str) -> str:
    """Collapse the column padding Obsidian adds to markdown tables.

    The knowledge index is ~240 KB on disk but most of it is spaces and
    dashes used to align columns; collapsing them cuts the prompt by ~4x
    without losing any information.
    """
    lines = []
    for line in markdown_table.splitlines():
        line = re.sub(r"-{3,}", "---", line)
        line = re.sub(r" {2,}", " ", line)
        lines.append(line.rstrip())
    return "\n".join(lines)


# ── State management ──────────────────────────────────────────────────

def load_state() -> dict:
    """Load persistent state from state.json."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"ingested": {}, "query_count": 0, "last_lint": None, "total_cost": 0.0}


def save_state(state: dict) -> None:
    """Save state to state.json."""
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ── File hashing ──────────────────────────────────────────────────────

def file_hash(path: Path) -> str:
    """SHA-256 hash of a file (first 16 hex chars)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# ── Slug / naming ─────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Convert text to a filename-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


# ── Wikilink helpers ──────────────────────────────────────────────────

def extract_wikilinks(content: str) -> list[str]:
    """Extract all [[wikilinks]] from markdown content."""
    return re.findall(r"\[\[([^\]]+)\]\]", content)


def wiki_article_exists(link: str) -> bool:
    """Check if a wikilinked article exists on disk."""
    path = KNOWLEDGE_DIR / f"{link}.md"
    return path.exists()


# ── Wiki content helpers ──────────────────────────────────────────────

def read_wiki_index() -> str:
    """Read the knowledge base index file."""
    if INDEX_FILE.exists():
        return INDEX_FILE.read_text(encoding="utf-8")
    return "# Knowledge Base Index\n\n| Article | Summary | Compiled From | Updated |\n|---------|---------|---------------|---------|"


def read_all_wiki_content() -> str:
    """Read index + all wiki articles into a single string for context."""
    parts = [f"## INDEX\n\n{read_wiki_index()}"]

    for subdir in [CONCEPTS_DIR, CONNECTIONS_DIR, QA_DIR]:
        if not subdir.exists():
            continue
        for md_file in sorted(subdir.glob("*.md")):
            rel = md_file.relative_to(KNOWLEDGE_DIR)
            content = md_file.read_text(encoding="utf-8")
            parts.append(f"## {rel}\n\n{content}")

    return "\n\n---\n\n".join(parts)


def list_wiki_articles() -> list[Path]:
    """List all wiki article files."""
    articles = []
    for subdir in [CONCEPTS_DIR, CONNECTIONS_DIR, QA_DIR]:
        if subdir.exists():
            articles.extend(sorted(subdir.glob("*.md")))
    return articles


def list_raw_files() -> list[Path]:
    """List all daily log files."""
    if not DAILY_DIR.exists():
        return []
    return sorted(DAILY_DIR.glob("*.md"))


# ── Index helpers ─────────────────────────────────────────────────────

def count_inbound_links(target: str, exclude_file: Path | None = None) -> int:
    """Count how many wiki articles link to a given target."""
    count = 0
    for article in list_wiki_articles():
        if article == exclude_file:
            continue
        content = article.read_text(encoding="utf-8")
        if f"[[{target}]]" in content:
            count += 1
    return count


def get_article_word_count(path: Path) -> int:
    """Count words in an article, excluding YAML frontmatter."""
    content = path.read_text(encoding="utf-8")
    # Strip frontmatter
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:]
    return len(content.split())


def build_index_entry(rel_path: str, summary: str, sources: str, updated: str) -> str:
    """Build a single index table row."""
    link = rel_path.replace(".md", "")
    return f"| [[{link}]] | {summary} | {sources} | {updated} |"
