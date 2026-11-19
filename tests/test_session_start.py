from pathlib import Path

from conftest import ROOT, load_module

ss = load_module(ROOT / "hooks" / "session-start.py", "session_start")


def make_kb_index(n: int) -> str:
    header = (
        "# Knowledge Base Index\n\n"
        "| Article | Summary | Compiled From | Updated |\n|---|---|---|---|\n"
    )
    rows = []
    for i in range(n):
        day = f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}"
        rows.append(
            f"| [[concepts/article-{i:03d}]]      | Summary of article {i} with some padding words to be long enough      "
            f"| daily/{day}.md | {day} |"
        )
    return header + "\n".join(rows) + "\n"


def test_compact_kb_index_keeps_most_recent_entries_within_budget():
    index = make_kb_index(433)
    out = ss.compact_kb_index(index, budget=4000)
    assert len(out) <= 4000
    lines = [l for l in out.splitlines() if l.startswith("- [[")]
    assert lines, "expected at least one compact entry"
    # newest first: the first entry's date must be >= the last entry's date
    dates = [l.rsplit("(", 1)[1].rstrip(")") for l in lines]
    assert dates == sorted(dates, reverse=True)
    assert "433 articles" in out  # tells the model the full index exists


def test_build_context_fits_under_claude_code_cap_with_every_section():
    kb_index = make_kb_index(600)
    daily = "# Daily Log: 2026-10-19\n\n### Session (10:00)\n\n**Context:** fixing the compiler\n" + ("- detail line of the log\n" * 2000)
    ctx = ss.build_context(kb_index=kb_index, recent_log=daily + "\nlast line marker\n", wiki_count=130)
    # Claude Code replaces anything over 10k chars with a 2k preview.
    assert len(ctx) <= ss.MAX_CONTEXT_CHARS < ss.HARD_LIMIT
    assert "qmd query" in ctx
    assert "600 articles" in ctx and "130 pages" in ctx
    assert "## Newest knowledge base articles" in ctx
    assert "- [[concepts/article-" in ctx
    assert "last line marker" in ctx  # the tail of the log, not its head


def test_build_context_without_knowledge_base():
    ctx = ss.build_context(kb_index=None, recent_log="(no recent daily log)")
    assert "qmd query" in ctx and "Newest knowledge base" not in ctx


def test_recent_log_looks_back_over_a_weekend(tmp_path):
    from datetime import datetime, timezone
    (tmp_path / "2026-10-16.md").write_text("# Daily Log: 2026-10-16\nfriday content\n")
    monday = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
    assert "friday content" in ss.get_recent_log(daily_dir=tmp_path, today=monday)


def test_recent_log_gives_up_after_lookback(tmp_path):
    from datetime import datetime, timezone
    (tmp_path / "2026-10-01.md").write_text("old\n")
    monday = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
    assert ss.get_recent_log(daily_dir=tmp_path, today=monday) == "(no recent daily log)"
