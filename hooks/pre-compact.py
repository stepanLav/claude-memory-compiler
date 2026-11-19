"""
PreCompact hook - captures the conversation before auto-compaction.

Compaction summarizes and discards detail, so the turns not flushed yet are
extracted now. The later SessionEnd flush continues from the offset this one
reaches, so nothing is summarized twice. See hooks/_capture.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _capture import run_capture  # noqa: E402

MIN_TURNS_TO_FLUSH = 5

if __name__ == "__main__":
    run_capture(event="PreCompact", log_tag="pre-compact", file_prefix="flush-context",
                min_turns=MIN_TURNS_TO_FLUSH)
