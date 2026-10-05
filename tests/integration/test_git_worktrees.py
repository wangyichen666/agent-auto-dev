"""不创建任何提交：Git >=2.42 的 orphan worktree 支持真实隔离测试。"""

import os
import shutil
from dataclasses import replace

import pytest

from dtcoder_agentic_dev.config import GitConfig, RepoConfig, WorkspaceConfig
from dtcoder_agentic_dev.infrastructure.git.client import GitClient
from dtcoder_agentic_dev.infrastructure.git.workspace import GitWorktreeWorkspaceManager
from dtcoder_agentic_dev.infrastructure.process.command import SubprocessCommandRunner


def test_two_real_worktrees_recover_and_cleanup(tmp_path, run, mocker):
    binary = os.environ.get("DTCODER_TEST_GIT") or shutil.which("git")
    if not binary:
        pytest.skip("没有 Git 可执行文件")
    commands = SubprocessCommandRunner()
    help_result = commands.run([binary, "worktree", "-h"], check=False)
    if "--orphan" not in help_result.stdout + help_result.stderr:
        pytest.skip("无提交测试需要 Git >=2.42；可通过 DTCODER_TEST_GIT 指定较新 Git")
    mirror = tmp_path / "mirrors/repo.git"
    mirror.parent.mkdir()
    commands.run([binary, "init", "--bare", str(mirror)])
    git = GitClient(commands, GitConfig(binary=binary))
    # 不创建提交、不访问远端；仅同步和基准查询使用替身。
    mocker.patch.object(git, "fetch")
    mocker.patch.object(git, "resolve_base", return_value="unborn-fixture")

    def add_orphan(mirror, path, branch, base):
        commands.run(
            [binary, "worktree", "add", "--orphan", "-b", branch, str(path)], cwd=str(mirror)
        )

    mocker.patch.object(git, "add_worktree", side_effect=add_orphan)
    manager = GitWorktreeWorkspaceManager(
        git, WorkspaceConfig(str(tmp_path / "mirrors"), str(tmp_path / "workspaces"))
    )
    a = replace(run, branch="agent/a", context={})
    b = replace(run, run_id="run-2", branch="agent/b", context={})
    repo = RepoConfig("r", "unused-local-fixture")
    a.workspace_path = manager.prepare(a, repo)
    b.workspace_path = manager.prepare(b, repo)
    assert a.workspace_path != b.workspace_path
    assert git.is_repository(a.workspace_path)
    assert git.current_branch(a.workspace_path) == "agent/a"
    (tmp_path / "workspaces/repo/run-1/a.txt").write_text("独立文件", encoding="utf-8")
    assert "a.txt" in git.snapshot_untracked(a.workspace_path)
    assert not (tmp_path / "workspaces/repo/run-2/a.txt").exists()
    assert manager.recover(a, repo) == a.workspace_path
    manager.cleanup(a)
    assert not (tmp_path / "workspaces/repo/run-1").exists()
    assert (tmp_path / "workspaces/repo/run-2").exists()
    manager.cleanup(b)
