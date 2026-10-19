import re

import compile as compiler  # scripts/compile.py


def test_prompt_does_not_inline_article_bodies():
    articles = {"concepts/a.md": "---\ntitle: A\n---\nSECRET_ARTICLE_BODY_A"}
    prompt = compiler.build_prompt(
        log_name="2026-10-07.md",
        log_content="### Session (10:00)\n\n**Context:** x",
        schema="SCHEMA",
        wiki_index="| [[concepts/a]] | sum | daily/x.md | 2026-01-01 |",
        domain_wiki_index="",
        existing_articles=articles,
        timestamp="2026-10-19T12:00:00+03:00",
    )
    assert "SECRET_ARTICLE_BODY_A" not in prompt
    assert "concepts/a.md" in prompt  # the slug list is still there
    assert "Read" in prompt  # the model is told to read articles on demand


def test_prompt_compresses_padded_index_columns():
    padded = "| [[concepts/a]]                     | sum                      | daily/x.md | 2026-01-01 |"
    prompt = compiler.build_prompt(
        log_name="l.md", log_content="c", schema="s", wiki_index=padded,
        domain_wiki_index="", existing_articles={}, timestamp="2026-10-19T12:00:00+03:00",
    )
    assert "| [[concepts/a]] | sum | daily/x.md | 2026-01-01 |" in prompt
    assert not re.search(r" {3,}", prompt.split("## Current Wiki Index")[1].split("##")[0])
