"""Path constants and configuration for the personal knowledge base."""

from pathlib import Path
from datetime import datetime, timezone

# ── Paths ──────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT_DIR / "daily"
KNOWLEDGE_DIR = ROOT_DIR / "knowledge"
CONCEPTS_DIR = KNOWLEDGE_DIR / "concepts"
CONNECTIONS_DIR = KNOWLEDGE_DIR / "connections"
QA_DIR = KNOWLEDGE_DIR / "qa"
REPORTS_DIR = ROOT_DIR / "reports"
SCRIPTS_DIR = ROOT_DIR / "scripts"
HOOKS_DIR = ROOT_DIR / "hooks"
AGENTS_FILE = ROOT_DIR / "AGENTS.md"

INDEX_FILE = KNOWLEDGE_DIR / "index.md"
LOG_FILE = KNOWLEDGE_DIR / "log.md"
STATE_FILE = SCRIPTS_DIR / "state.json"
COMPILE_LOCK_FILE = SCRIPTS_DIR / "compile.lock"

# ── Models ─────────────────────────────────────────────────────────────
# Pinned explicitly so the compiler never inherits the interactive `model`
# from ~/.claude/settings.json. The SDK ships its own Claude Code binary;
# a model alias that binary does not know makes every call exit with code 1
# (this took the pipeline down for Sep 21-23 and Oct 8-15, 2026).
FLUSH_MODEL = "opus"        # judges what is worth keeping; sonnet returned FLUSH_OK on a substantive session
COMPILE_MODEL = "opus"      # multi-step article writing with tools
FALLBACK_MODEL = "sonnet"   # used by the CLI when the primary is overloaded

# ── Concurrency ────────────────────────────────────────────────────────
SESSION_LOCK_WINDOW_SECONDS = 60     # same session flushed twice inside this window = duplicate
COMPILE_LOCK_STALE_SECONDS = 2 * 3600  # a compile lock older than this is treated as abandoned


def now_iso() -> str:
    """Current time in ISO 8601 format."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def today_iso() -> str:
    """Current date in ISO 8601 format."""
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
