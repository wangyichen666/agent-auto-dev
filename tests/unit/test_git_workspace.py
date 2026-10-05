from dataclasses import replace

import pytest

from dtcoder_agentic_dev.config import GitConfig, RepoConfig, WorkspaceConfig
from dtcoder_agentic_dev.domain.errors import FatalError
from dtcoder_agentic_dev.infrastructure.git.client import GitClient
from dtcoder_agentic_dev.infrastructure.git.workspace import GitWorktreeWorkspaceManager
from dtcoder_agentic_dev.ports.command_runner import CommandResult


def result(stdout="", returncode=0):
    return CommandResult((), returncode, stdout, "", 0)


def test_commit_bounded_and_push_mocked(tmp_path, mocker):
    commands = mocker.Mock()
    # 第一步列出新旧未跟踪文件；仅新增文件和明确产物进入暂存。
    commands.run.side_effect = [
        result("old.txt\0new.py\0"),
        result(),
        result(),
        result(returncode=1),
        result(),
    ]
    git = GitClient(commands, GitConfig())
    assert git.commit_changes(
        str(tmp_path), "提交说明", artifact_paths=[".agents/spec.md"], untracked_before={"old.txt"}
    )
    arrays = [call.args[0] for call in commands.run.call_args_list]
    assert ["git", "add", "-u", "--"] in arrays
    explicit = next(args for args in arrays if args[:3] == ["git", "add", "--"])
    assert "new.py" in explicit and "old.txt" not in explicit and ".agents/spec.md" in explicit
    assert not any("-A" in args for args in arrays)
    assert any("commit" in args for args in arrays)  # 仅 Mock，绝不执行真实提交。
    commands.run.side_effect = None
    commands.run.return_value = result()
    git.push(str(tmp_path), "agent/work")
    assert commands.run.call_args.args[0] == ["git", "push", "origin", "HEAD:refs/heads/agent/work"]


def test_add_worktree_and_clone_command(mocker, tmp_path):
    commands = mocker.Mock()
    commands.run.return_value = result(returncode=1)
    git = GitClient(commands, GitConfig())
    git.add_worktree(tmp_path, tmp_path / "run", "agent/run", "main")
    assert commands.run.call_args.args[0] == [
        "git",
        "worktree",
        "add",
        "-b",
        "agent/run",
        "--",
        str(tmp_path / "run"),
        "refs/remotes/origin/main",
    ]
    commands.run.return_value = result()
    git.clone_mirror("https://example.invalid/repo", tmp_path / "mirror")
    assert any("clone" in c.args[0] for c in commands.run.call_args_list)


def test_paths_and_cleanup_guard(mocker, tmp_path, run):
    git = mocker.Mock()
    manager = GitWorktreeWorkspaceManager(
        git, WorkspaceConfig(str(tmp_path / "mirrors"), str(tmp_path / "workspaces"))
    )
    with pytest.raises(FatalError):
        manager.prepare(replace(run, run_id="../unsafe"), RepoConfig("r", "remote"))
    with pytest.raises(FatalError):
        manager.cleanup(replace(run, workspace_path=str(tmp_path / "outside")))
    git.remove_worktree.assert_not_called()


def test_local_clone_source_restores_configured_remote(mocker, tmp_path, run):
    git = mocker.Mock()
    git.is_repository.return_value = False
    git.resolve_base.return_value = "base-sha"
    repo = RepoConfig(
        "r", "https://example.invalid/official.git", path=str(tmp_path / "local-source")
    )
    manager = GitWorktreeWorkspaceManager(
        git, WorkspaceConfig(str(tmp_path / "mirrors"), str(tmp_path / "workspaces"))
    )
    manager.prepare(run, repo)
    mirror = tmp_path / "mirrors/repo.git"
    git.clone_mirror.assert_called_once_with(repo.path, mirror)
    git.set_remote_url.assert_called_once_with(mirror, repo.remote)
    git.fetch.assert_called_once_with(mirror, run.base_branch)
