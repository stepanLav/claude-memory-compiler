"""
SessionEnd hook - captures the conversation for memory extraction.

Extracts every turn the previous flush of this session has not seen and spawns
flush.py in the background. See hooks/_capture.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _capture import run_capture  # noqa: E402

MIN_TURNS_TO_FLUSH = 6  # user+assistant messages; <3 exchanges is never worth an LLM call

if __name__ == "__main__":
    run_capture(event="SessionEnd", log_tag="hook", file_prefix="session-flush",
                min_turns=MIN_TURNS_TO_FLUSH)
