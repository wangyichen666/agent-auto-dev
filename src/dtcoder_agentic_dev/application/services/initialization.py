"""幂等初始化；配置结构化更新，内置模板覆盖前备份。"""

import os
import time
from importlib.resources import files
from pathlib import Path

import yaml

from dtcoder_agentic_dev.adapters.persistence.sqlite import SQLiteRunRepository
from dtcoder_agentic_dev.application.services.event_views import DEFAULT_TEMPLATES
from dtcoder_agentic_dev.config import DEFAULT_CONFIG, RepoConfig, load_config
from dtcoder_agentic_dev.domain.errors import ConfigurationError
from dtcoder_agentic_dev.infrastructure.filesystem.atomic import atomic_write


def initialize(
    path=None, *, repository=None, update_templates=False, prepare_mirror=False, commands=None
):
    target = Path(os.path.expandvars(str(path or DEFAULT_CONFIG))).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        atomic_write(
            target,
            files("dtcoder_agentic_dev.resources")
            .joinpath("config.example.yaml")
            .read_text(encoding="utf-8"),
        )
    if repository:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
        # 已有配置也必须先严格校验。
        load_config(target)
        repos = data.setdefault("repositories", [])
        provider = repository.get("provider") or data.get("code_host", {}).get("adapter", "logging")
        identity = RepoConfig(
            repository.get("name", ""),
            repository.get("remote", ""),
            provider=provider,
            project=repository.get("project", ""),
        ).repository_id
        existing = next(
            (
                r
                for r in repos
                if RepoConfig(
                    r["name"],
                    r["remote"],
                    provider=r.get("provider", "logging"),
                    project=r.get("project", ""),
                ).repository_id
                == identity
            ),
            None,
        )
        if existing is None and not repository.get("remote") and not repository.get("project"):
            existing = next((r for r in repos if r["name"] == repository.get("name")), None)
            if existing:
                provider = existing.get("provider", "logging")
        if existing is None:
            if not repository.get("name") or not repository.get("remote"):
                raise ConfigurationError("新增仓库需要 --name 和 --remote")
            existing = {"provider": provider}
            repos.append(existing)
        repository = dict(repository)
        if "pipeline" in repository:
            repository["pipeline"] = {**existing.get("pipeline", {}), **repository["pipeline"]}
        existing.update(repository)
        existing["provider"] = provider
        for key in ("code_host_adapter", "pipeline_adapter"):
            if key in existing:
                section = "code_host" if key == "code_host_adapter" else "pipeline"
                data.setdefault(section, {})["adapter"] = existing.pop(key)
        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        check = target.parent / f".config-check-{time.time_ns()}.yaml"
        try:
            check.write_text(text, encoding="utf-8")
            load_config(check)
        finally:
            check.unlink(missing_ok=True)
        atomic_write(target, text)
    config = load_config(target)
    for directory in (
        config.state.directory,
        config.workspace.mirrors,
        config.workspace.workspaces,
        config.prompts.directory,
        config.notification.templates_directory,
        str(Path(config.state.directory) / "logs/runs"),
        config.declarative.directory,
        config.declarative.skills_directory,
    ):
        Path(directory).mkdir(parents=True, exist_ok=True)
    resource = files("dtcoder_agentic_dev.prompts").joinpath("templates")
    templates = [
        (
            Path(config.prompts.directory) / f"{name}.txt",
            resource.joinpath(f"{name}.txt").read_text(encoding="utf-8"),
        )
        for name in (
            "requirements",
            "coding",
            "code_review",
            "fix_loop",
            "pr_create",
            "agent_zh",
            "agent_en",
        )
    ]
    templates += [
        (Path(config.notification.templates_directory) / f"{name}.txt", text)
        for name, text in DEFAULT_TEMPLATES.items()
    ]
    templates += [
        (
            Path(config.declarative.directory) / f"{name}.yaml",
            files("dtcoder_agentic_dev.resources")
            .joinpath(f"workflow.{name}.yaml")
            .read_text(encoding="utf-8"),
        )
        for name in ("document", "local-files")
    ]
    for destination, text in templates:
        if destination.is_symlink():
            raise ConfigurationError("拒绝写入符号链接模板")
        if destination.exists() and not update_templates:
            continue
        if destination.exists():
            destination.with_name(destination.name + f".backup-{time.time_ns()}").write_bytes(
                destination.read_bytes()
            )
        atomic_write(destination, text)
    database = SQLiteRunRepository(config.state.database)
    database.close()
    (Path(config.state.directory) / "logs/scheduler.log").touch(exist_ok=True)
    if prepare_mirror and not config.dry_run:
        from dtcoder_agentic_dev.infrastructure.git.client import GitClient
        from dtcoder_agentic_dev.infrastructure.locking.file import file_lock
        from dtcoder_agentic_dev.infrastructure.process.command import SubprocessCommandRunner

        git = GitClient(commands or SubprocessCommandRunner(), config.git)
        for repo in config.repositories:
            mirror = Path(config.workspace.mirrors) / f"{repo.repository_id}.git"
            with file_lock(mirror.parent / f"{repo.repository_id}.lock"):
                if not git.is_repository(mirror):
                    git.clone_mirror(repo.path or repo.remote, mirror)
                    git.set_remote_url(mirror, repo.remote)
                git.fetch(mirror, repo.base_branch)
    return target
