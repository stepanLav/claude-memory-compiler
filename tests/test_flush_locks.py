import os
import time

import flush


def test_session_lock_first_caller_wins(tmp_path):
    assert flush.acquire_session_lock("sid-1", tmp_path) is True
    assert flush.acquire_session_lock("sid-1", tmp_path) is False


def test_session_lock_expires_after_window(tmp_path):
    assert flush.acquire_session_lock("sid-2", tmp_path, window=60) is True
    lock = next(tmp_path.glob("*sid-2*"))
    old = time.time() - 120
    os.utime(lock, (old, old))
    assert flush.acquire_session_lock("sid-2", tmp_path, window=60) is True


def test_different_sessions_do_not_block_each_other(tmp_path):
    assert flush.acquire_session_lock("a", tmp_path) is True
    assert flush.acquire_session_lock("b", tmp_path) is True


def test_compile_lock_is_exclusive_and_releasable(tmp_path):
    lock = tmp_path / "compile.lock"
    assert flush.acquire_compile_lock(lock) is True
    assert flush.acquire_compile_lock(lock) is False
    flush.release_compile_lock(lock)
    assert flush.acquire_compile_lock(lock) is True


def test_compile_lock_stale_is_reclaimed(tmp_path):
    lock = tmp_path / "compile.lock"
    assert flush.acquire_compile_lock(lock, stale_seconds=3600) is True
    old = time.time() - 7200
    os.utime(lock, (old, old))
    assert flush.acquire_compile_lock(lock, stale_seconds=3600) is True
