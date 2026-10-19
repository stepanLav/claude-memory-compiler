import subprocess
from pathlib import Path

import datasync


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    work = tmp_path / "data"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "t")
    (work / "knowledge").mkdir()
    (work / "knowledge" / "index.md").write_text("# index\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "init")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-q", "-u", "origin", "main")
    return work, remote


def test_sync_commits_and_pushes_changes(tmp_path):
    work, remote = make_repo_with_remote(tmp_path)
    (work / "knowledge" / "new.md").write_text("new article\n")
    result = datasync.sync_data_repo(work / "knowledge", forbidden_root=tmp_path / "compiler")
    assert result.startswith("pushed")
    assert git(work, "status", "--porcelain") == ""
    assert git(work, "rev-parse", "HEAD") == git(remote, "rev-parse", "main")


def test_sync_is_a_noop_without_changes(tmp_path):
    work, _ = make_repo_with_remote(tmp_path)
    before = git(work, "rev-parse", "HEAD")
    result = datasync.sync_data_repo(work / "knowledge", forbidden_root=tmp_path / "compiler")
    assert result == "nothing to commit"
    assert git(work, "rev-parse", "HEAD") == before


def test_sync_refuses_when_data_root_is_the_compiler_repo(tmp_path):
    work, _ = make_repo_with_remote(tmp_path)
    (work / "knowledge" / "new.md").write_text("x\n")
    before = git(work, "rev-parse", "HEAD")
    result = datasync.sync_data_repo(work / "knowledge", forbidden_root=work)
    assert result.startswith("skipped")
    assert git(work, "rev-parse", "HEAD") == before


def test_sync_skips_plain_directory(tmp_path):
    plain = tmp_path / "plain"
    (plain / "knowledge").mkdir(parents=True)
    result = datasync.sync_data_repo(plain / "knowledge", forbidden_root=tmp_path / "compiler")
    assert result.startswith("skipped")


def test_sync_follows_symlink_to_data_repo(tmp_path):
    work, remote = make_repo_with_remote(tmp_path)
    compiler = tmp_path / "compiler"
    compiler.mkdir()
    (compiler / "knowledge").symlink_to(work / "knowledge")
    (work / "knowledge" / "via-link.md").write_text("x\n")
    result = datasync.sync_data_repo(compiler / "knowledge", forbidden_root=compiler)
    assert result.startswith("pushed")
    assert git(work, "rev-parse", "HEAD") == git(remote, "rev-parse", "main")
