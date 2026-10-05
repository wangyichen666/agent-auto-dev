"""所有 Git 命令集中在此；暂存范围有明确边界。"""

from pathlib import Path

from dtcoder_agentic_dev.config import validate_ref
from dtcoder_agentic_dev.domain.errors import TechnicalError
from dtcoder_agentic_dev.infrastructure.filesystem.artifacts import contained_path


class GitClient:
    def __init__(self, commands, config):
        self.commands, self.config = commands, config

    def _run(self, path, *args, check=True):
        return self.commands.run(
            [self.config.binary, *args], cwd=str(path), timeout=self.config.timeout, check=check
        )

    def is_repository(self, path):
        if not Path(path).is_dir():
            return False
        return self._run(path, "rev-parse", "--git-dir", check=False).returncode == 0

    def clone_mirror(self, remote, path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.commands.run(
            [self.config.binary, "clone", "--mirror", "--", remote, str(target)],
            timeout=self.config.timeout,
        )
        # 保留镜像本地工作分支，仅同步到 remote namespace，避免覆盖被 worktree 使用的分支。
        self._run(target, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        self._run(target, "config", "remote.origin.mirror", "false")

    def fetch(self, mirror, base_branch):
        validate_ref(base_branch)
        self._run(
            mirror,
            "fetch",
            "--prune",
            "origin",
            f"+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}",
        )

    def set_remote_url(self, mirror, remote):
        self._run(mirror, "remote", "set-url", "origin", remote)

    def branch_exists(self, path, branch):
        validate_ref(branch)
        return (
            self._run(path, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode
            == 0
        )

    def add_worktree(self, mirror, path, branch, base):
        validate_ref(branch)
        validate_ref(base)
        if self.branch_exists(mirror, branch):
            self._run(mirror, "worktree", "add", "--", str(path), branch)
        else:
            self._run(
                mirror,
                "worktree",
                "add",
                "-b",
                branch,
                "--",
                str(path),
                f"refs/remotes/origin/{base}",
            )

    def remove_worktree(self, mirror, path):
        self._run(mirror, "worktree", "remove", "--force", "--", str(path))
        self._run(mirror, "worktree", "prune")

    def current_branch(self, path):
        return self._run(path, "symbolic-ref", "--short", "HEAD").stdout.strip()

    def status_porcelain(self, path):
        return self._run(path, "status", "--porcelain=v1", "--untracked-files=all").stdout

    def reset_and_clean(self, path):
        self._run(path, "reset", "--hard", "HEAD")
        self._run(path, "clean", "-fd")

    def snapshot_untracked(self, path):
        output = self._run(path, "ls-files", "--others", "--exclude-standard", "-z").stdout
        return {name for name in output.split("\0") if name}

    def commit_changes(self, path, message, *, artifact_paths, untracked_before):
        paths = set(artifact_paths) | (self.snapshot_untracked(path) - untracked_before)
        for name in paths:
            contained_path(str(path), name)
        self._run(path, "add", "-u", "--")
        if paths:
            self._run(path, "add", "--", *sorted(paths))
        check = self._run(path, "diff", "--cached", "--quiet", check=False)
        if check.returncode == 0:
            return False
        if check.returncode != 1:
            raise TechnicalError("无法检查暂存变更")
        self._run(
            path,
            "-c",
            f"user.name={self.config.author_name}",
            "-c",
            f"user.email={self.config.author_email}",
            "commit",
            "-m",
            message,
        )
        return True

    def push(self, path, branch):
        validate_ref(branch)
        self._run(path, "push", "origin", f"HEAD:refs/heads/{branch}")

    def head_sha(self, path):
        return self._run(path, "rev-parse", "HEAD").stdout.strip()

    def diff_name_only(self, path, base):
        # 基础分支保留为隔离 workspace 创建时的 commit SHA，避免后续 fetch 改变评审基准。
        if base.startswith("-"):
            raise TechnicalError("Git diff 基础引用无效")
        output = self._run(path, "diff", "--name-only", "-z", f"{base}...HEAD", "--").stdout
        return [name for name in output.split("\0") if name]

    def resolve_base(self, mirror, base):
        validate_ref(base)
        return self._run(mirror, "rev-parse", f"refs/remotes/origin/{base}").stdout.strip()
