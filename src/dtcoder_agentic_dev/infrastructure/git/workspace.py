import re
from pathlib import Path

from dtcoder_agentic_dev.domain.errors import FatalError
from dtcoder_agentic_dev.infrastructure.filesystem.workspace import LocalTaskWorkspace
from dtcoder_agentic_dev.infrastructure.locking.file import file_lock


class GitWorktreeWorkspaceManager:
    def __init__(self, git, config):
        self.git, self.config = git, config

    def _paths(self, run):
        for key in (run.repository_id, run.run_id):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
                raise FatalError("运行或仓库 ID 不能用于安全的工作区路径")
        mirror = Path(self.config.mirrors).resolve() / f"{run.repository_id}.git"
        workspace = Path(self.config.workspaces).resolve() / run.repository_id / run.run_id
        mirror_root, workspace_root = (
            Path(self.config.mirrors).resolve(),
            Path(self.config.workspaces).resolve(),
        )
        if not mirror.resolve().is_relative_to(
            mirror_root
        ) or not workspace.resolve().is_relative_to(workspace_root):
            raise FatalError("受管工作区或镜像存在路径逃逸")
        return mirror, workspace

    def prepare(self, run, repository):
        if run.context.get("rollback_revision"):
            return self.prepare_at(run, repository, run.context["rollback_revision"])
        mirror, workspace = self._paths(run)
        with file_lock(mirror.parent / f"{run.repository_id}.lock"):
            if not self.git.is_repository(mirror):
                self.git.clone_mirror(repository.path or repository.remote, mirror)
                if repository.path:
                    self.git.set_remote_url(mirror, repository.remote)
            self.git.fetch(mirror, run.base_branch)
            if workspace.exists():
                return self._recover(run, workspace)
            workspace.parent.mkdir(parents=True, exist_ok=True)
            self.git.add_worktree(mirror, workspace, run.branch, run.base_branch)
            run.context.setdefault("base_revision", self.git.resolve_base(mirror, run.base_branch))
            return str(workspace)

    def _recover(self, run, workspace):
        if (
            not self.git.is_repository(workspace)
            or self.git.current_branch(workspace) != run.branch
        ):
            raise FatalError("已有工作区不属于此运行，拒绝自动重置")
        if run.workspace_path and Path(run.workspace_path).resolve() != workspace:
            raise FatalError("运行记录的工作区路径与受管路径不一致")
        if not run.context.get("base_revision"):
            run.context["base_revision"] = self.git.head_sha(workspace)
        return str(workspace)

    def recover(self, run, repository):
        mirror, workspace = self._paths(run)
        if not workspace.exists():
            return self.prepare(run, repository)
        with file_lock(mirror.parent / f"{run.repository_id}.lock"):
            return self._recover(run, workspace)

    def cleanup(self, run):
        if run.context.get("repo_mode") == "none" and not run.repository_id:
            return LocalTaskWorkspace(self.config.workspaces).cleanup(run)
        mirror, workspace = self._paths(run)
        if run.workspace_path and Path(run.workspace_path).resolve() != workspace:
            raise FatalError("拒绝清理非当前运行的工作区")
        with file_lock(mirror.parent / f"{run.repository_id}.lock"):
            if workspace.exists():
                self._recover(run, workspace)
                self.git.remove_worktree(mirror, workspace)

    def prepare_at(self, run, repository, revision):
        mirror, workspace = self._paths(run)
        with file_lock(mirror.parent / f"{run.repository_id}.lock"):
            if not self.git.is_repository(mirror):
                raise FatalError("回退需要已有受管镜像")
            if workspace.exists():
                return self._recover(run, workspace)
            workspace.parent.mkdir(parents=True, exist_ok=True)
            if self.git.branch_exists(mirror, run.branch):
                self.git.add_worktree(mirror, workspace, run.branch, run.base_branch)
                if self.git.head_sha(workspace) != revision and not self.git.is_ancestor(
                    workspace, revision
                ):
                    raise FatalError("已存在后继分支与目标修订不一致")
            else:
                self.git.add_worktree_at(mirror, workspace, run.branch, revision)
            return str(workspace)
