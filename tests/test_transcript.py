import asyncio
import json

import flush
import transcript as tr


def line(role, content, **extra):
    return json.dumps({"type": role, "message": {"role": role, "content": content}, **extra}) + "\n"


def write_transcript(path, lines):
    path.write_text("".join(lines), encoding="utf-8")


def test_extracts_whole_conversation_not_just_the_tail(tmp_path):
    t = tmp_path / "s.jsonl"
    write_transcript(t, [line("user", f"question {i} " + "x" * 500) for i in range(200)])
    turns, end = tr.extract_turns(t)
    assert len(turns) == 200
    assert "question 0 " in turns[0]
    assert end == t.stat().st_size


def test_skips_noise_and_keeps_tool_notes(tmp_path):
    t = tmp_path / "s.jsonl"
    write_transcript(t, [
        line("user", "<system-reminder>ignore me</system-reminder>real question"),
        line("user", "subagent chatter", isSidechain=True),
        line("user", "compaction summary", isCompactSummary=True),
        line("assistant", [{"type": "text", "text": "looking"},
                           {"type": "tool_use", "name": "Bash", "input": {"description": "Run tests", "command": "pytest"}}]),
        line("assistant", [{"type": "tool_use", "name": "Read", "input": {"file_path": "/a/b.py"}}]),
        line("user", [{"type": "tool_result", "content": "huge output"}]),
        json.dumps({"type": "attachment", "x": 1}) + "\n",
    ])
    turns, _ = tr.extract_turns(t)
    assert turns[0] == "**User:** real question"
    assert len(turns) == 2
    assert "[tool: Bash — Run tests]" in turns[1] and "[tool: Read — /a/b.py]" in turns[1]
    assert "huge output" not in "".join(turns) and "chatter" not in "".join(turns)


def test_incremental_extraction_resumes_at_offset(tmp_path):
    t = tmp_path / "s.jsonl"
    write_transcript(t, [line("user", "first")])
    turns, end = tr.extract_turns(t)
    assert turns == ["**User:** first"]
    with open(t, "a", encoding="utf-8") as f:
        f.write(line("assistant", "second"))
        f.write('{"type": "user", "message": {"role": "user", "content": "half-written')  # no newline yet
    turns, end2 = tr.extract_turns(t, end)
    assert turns == ["**Assistant:** second"]
    assert end2 < t.stat().st_size  # the partial line is left for the next flush


def test_offset_roundtrip_and_reset_when_transcript_shrinks(tmp_path):
    assert tr.read_offset(tmp_path, "sid") == 0
    tr.write_offset(tmp_path, "sid", 1234)
    assert tr.read_offset(tmp_path, "sid") == 1234
    t = tmp_path / "s.jsonl"
    write_transcript(t, [line("user", "only")])
    turns, _ = tr.extract_turns(t, start_offset=10_000)
    assert turns == ["**User:** only"]


def test_long_turn_is_clipped_in_the_middle(tmp_path):
    t = tmp_path / "s.jsonl"
    write_transcript(t, [line("user", "START" + "y" * 50_000 + "END")])
    turns, _ = tr.extract_turns(t)
    assert len(turns[0]) < tr.MAX_TURN_CHARS + 200
    assert "START" in turns[0] and turns[0].endswith("END") and "chars omitted" in turns[0]


def test_chunks_keep_turns_whole():
    context = tr.join_turns([f"**User:** turn {i} " + "z" * 900 for i in range(10)])
    chunks = tr.split_into_chunks(context, 3000)
    assert len(chunks) > 1
    assert all(len(c) <= 3000 for c in chunks)
    assert sum(c.count("**User:** turn") for c in chunks) == 10


def test_short_session_is_one_call():
    calls = []

    async def fake(prompt):
        calls.append(prompt)
        return "**Context:** short"

    out = asyncio.run(flush.run_flush(tr.join_turns(["**User:** hi", "**Assistant:** hello"]), ask_fn=fake))
    assert out == "**Context:** short" and len(calls) == 1
    assert "**User:** hi" in calls[0]


def test_long_session_is_mapped_then_merged():
    calls = []

    async def fake(prompt):
        calls.append(prompt)
        if "## Notes" in prompt:
            return "**Context:** merged"
        return "NOTHING" if "turn 0 " in prompt else "note"

    context = tr.join_turns([f"**User:** turn {i} " + "z" * 900 for i in range(6)])
    out = asyncio.run(flush.run_flush(context, chunk_chars=2000, ask_fn=fake))
    assert out == "**Context:** merged"
    assert len(calls) > 3 and "## Notes" in calls[-1]
    assert "NOTHING" not in calls[-1]  # empty parts are dropped before the merge


def test_failed_call_becomes_flush_error():
    async def boom(prompt):
        raise flush.FlushError("API down")

    out = asyncio.run(flush.run_flush("**User:** hi", ask_fn=boom))
    assert out.startswith("FLUSH_ERROR") and "API down" in out
