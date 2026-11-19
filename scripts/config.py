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

# ── Agent isolation ────────────────────────────────────────────────────
# Flush/compile/lint/query agents must not load the interactive setup: with the
# SDK default (user + project settings) every flush received the SessionStart
# injection twice plus the full skill listing - ~31k tokens of context around a
# ~4k-token payload - and left a transcript in ~/.claude/projects per call.
AGENT_SETTING_SOURCES: list[str] = []
AGENT_EXTRA_ARGS: dict[str, str | None] = {"no-session-persistence": None}

# ── Flush sizing ───────────────────────────────────────────────────────
# A session is summarized whole. Up to this many characters go in one call;
# longer sessions are summarized chunk by chunk and then merged.
FLUSH_CHUNK_CHARS = 120_000

# ── Search index ───────────────────────────────────────────────────────
# qmd (https://github.com/tobi/qmd) indexes wiki/, knowledge/ and daily/.
# Refreshed after each flush (keyword index) and each compile (+ embeddings).
QMD_BIN = "qmd"

# ── Concurrency ────────────────────────────────────────────────────────
SESSION_LOCK_WINDOW_SECONDS = 60     # same session flushed twice inside this window = duplicate
COMPILE_LOCK_STALE_SECONDS = 2 * 3600  # a compile lock older than this is treated as abandoned

# ── Data repo sync ─────────────────────────────────────────────────────
# After a compile, commit + push the git repo that owns knowledge/ (and daily/),
# if that is a separate repo reached through symlinks. Never syncs the compiler
# checkout itself - see scripts/datasync.py.
AUTO_SYNC_DATA_REPO = True


def now_iso() -> str:
    """Current time in ISO 8601 format."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def today_iso() -> str:
    """Current date in ISO 8601 format."""
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
