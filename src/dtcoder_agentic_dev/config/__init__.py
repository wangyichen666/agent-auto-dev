"""严格 dataclass 配置；配置文件目录决定未指定的运行目录。"""

import hashlib
import math
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints
from urllib.parse import urlsplit

import yaml

from dtcoder_agentic_dev.domain.errors import ConfigurationError


@dataclass
class SchedulerConfig:
    poll_interval: float = 30
    lease_seconds: float = 120
    heartbeat_interval: float = 15
    max_nodes_per_tick: int = 20


@dataclass
class CodexConfig:
    binary: str = "codex"
    extra_args: list[str] = field(default_factory=list)
    output_format: str = "text"
    timeout: float = 1800


@dataclass
class StateConfig:
    directory: str = ""
    database: str = ""


@dataclass
class WorkspaceConfig:
    mirrors: str = ""
    workspaces: str = ""


@dataclass
class GitConfig:
    binary: str = "git"
    timeout: float = 300
    author_name: str = "DTCoder"
    author_email: str = "dtcoder@example.invalid"


@dataclass
class PromptConfig:
    directory: str = ""


@dataclass
class CodeHostConfig:
    adapter: str = "logging"
    token_env: str = ""


@dataclass
class PipelineConfig:
    adapter: str = "disabled"
    poll_interval: float = 30


@dataclass
class NotificationConfig:
    adapter: str = "null"
    issue_comments: bool = False


@dataclass
class WorkflowConfig:
    name: str = "issue-development"
    version: str = "1"
    max_retries: int = 2
    retry_delay: float = 1
    max_fix_loops: int = 3


@dataclass
class RepoConfig:
    name: str
    remote: str
    provider: str = "logging"
    project: str = ""
    base_branch: str = "main"
    path: str | None = None
    labels: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    validation_commands: list[list[str]] = field(default_factory=list)
    validation_timeout: float = 300
    pipeline_enabled: bool = False
    pipeline_failure_blocks: bool = True
    repository_id: str = ""

    def __post_init__(self):
        identity = f"{self.provider}\0{self.project or self.remote}"
        self.repository_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


@dataclass
class AppConfig:
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    state: StateConfig = field(default_factory=StateConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    git: GitConfig = field(default_factory=GitConfig)
    prompts: PromptConfig = field(default_factory=PromptConfig)
    code_host: CodeHostConfig = field(default_factory=CodeHostConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    notification: NotificationConfig = field(default_factory=NotificationConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    repositories: list[RepoConfig] = field(default_factory=list)
    dry_run: bool = False


DEFAULT_CONFIG = Path("~/.dtcoder-agentic-dev/config.yaml").expanduser()
SECTIONS = {
    "scheduler": SchedulerConfig,
    "codex": CodexConfig,
    "state": StateConfig,
    "workspace": WorkspaceConfig,
    "git": GitConfig,
    "prompts": PromptConfig,
    "code_host": CodeHostConfig,
    "pipeline": PipelineConfig,
    "notification": NotificationConfig,
    "workflow": WorkflowConfig,
}


def _expand(value):
    if isinstance(value, str):
        return os.path.expanduser(os.path.expandvars(value))
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _matches(value, hint):
    origin = get_origin(hint)
    if origin is list:
        return isinstance(value, list) and all(_matches(v, get_args(hint)[0]) for v in value)
    if get_args(hint):
        return any(_matches(value, h) for h in get_args(hint))
    if hint is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, hint) and (hint is not int or not isinstance(value, bool))


def _construct(cls, data):
    if not isinstance(data, dict):
        raise ConfigurationError(f"{cls.__name__} 必须是映射")
    hints = get_type_hints(cls)
    unknown = set(data) - {f.name for f in fields(cls)}
    if unknown:
        raise ConfigurationError(f"{cls.__name__} 存在未知字段：{', '.join(sorted(unknown))}")
    for name, value in data.items():
        if not _matches(value, hints[name]):
            raise ConfigurationError(f"{cls.__name__}.{name} 类型错误")
    try:
        return cls(**data)
    except TypeError as exc:
        raise ConfigurationError(f"{cls.__name__} 缺少必填字段") from exc


def _path(value: str, base: Path) -> str:
    path = Path(value)
    return str((path if path.is_absolute() else base / path).resolve())


def validate_ref(value: str) -> None:
    if (
        not value
        or value.startswith(("-", "/"))
        or value.endswith(("/", ".", ".lock"))
        or ".." in value
        or "@{" in value
        or "//" in value
        or re.search(r"[\x00-\x20~^:?*\[\\]", value)
    ):
        raise ConfigurationError("分支名无效")


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path or DEFAULT_CONFIG).expanduser().resolve()
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"无法读取配置：{config_path}；请先执行 init") from exc
    if not isinstance(data, dict):
        raise ConfigurationError("配置顶层必须是映射")
    unknown = set(data) - set(SECTIONS) - {"repositories", "dry_run"}
    if unknown:
        raise ConfigurationError(f"未知配置项：{', '.join(sorted(unknown))}")
    data = _expand(data)
    config = AppConfig()
    for name, cls in SECTIONS.items():
        setattr(config, name, _construct(cls, data.get(name, {})))
    if not isinstance(data.get("repositories", []), list):
        raise ConfigurationError("repositories 必须是列表")
    config.repositories = [_construct(RepoConfig, r) for r in data.get("repositories", [])]
    if type(data.get("dry_run", False)) is not bool:
        raise ConfigurationError("dry_run 必须是布尔值")
    config.dry_run = data.get("dry_run", False)
    base = config_path.parent
    config.state.directory = _path(config.state.directory or ".", base)
    state_base = Path(config.state.directory)
    config.state.database = _path(config.state.database or "state.db", state_base)
    config.workspace.mirrors = _path(config.workspace.mirrors or "mirrors", state_base)
    config.workspace.workspaces = _path(config.workspace.workspaces or "workspaces", state_base)
    config.prompts.directory = _path(config.prompts.directory or "prompts", base)
    positive = [
        (config.scheduler.poll_interval, "轮询间隔"),
        (config.scheduler.lease_seconds, "租约时长"),
        (config.scheduler.heartbeat_interval, "续租间隔"),
        (config.scheduler.max_nodes_per_tick, "单次最大节点数"),
        (config.codex.timeout, "Codex 超时"),
        (config.git.timeout, "Git 超时"),
        (config.pipeline.poll_interval, "流水线轮询间隔"),
    ]
    for value, name in positive:
        if not math.isfinite(value) or value <= 0:
            raise ConfigurationError(f"{name} 必须大于 0")
    if config.scheduler.heartbeat_interval >= config.scheduler.lease_seconds / 2:
        raise ConfigurationError("续租间隔必须小于租约时长的一半")
    if (
        config.workflow.max_retries < 0
        or config.workflow.max_fix_loops < 0
        or not math.isfinite(config.workflow.retry_delay)
        or config.workflow.retry_delay < 0
    ):
        raise ConfigurationError("重试、修复次数和退避时间不能为负")
    if config.workflow.name != "issue-development" or config.workflow.version != "1":
        raise ConfigurationError("当前产品仅装配 issue-development 版本 1；扩展产品请增加装配")
    if not config.codex.binary or not config.git.binary:
        raise ConfigurationError("Git 和 Codex binary 不能为空")
    if config.codex.output_format not in {"text", "json"}:
        raise ConfigurationError("Codex output_format 必须是 text 或 json")
    for adapter_name in (
        config.code_host.adapter,
        config.notification.adapter,
        config.pipeline.adapter,
    ):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", adapter_name):
            raise ConfigurationError("适配器名称必须是非空标识符")
    identities = set()
    names = set()
    for repo in config.repositories:
        if (
            not repo.name.strip()
            or not repo.provider.strip()
            or not repo.remote.strip()
            or repo.remote.startswith("-")
        ):
            raise ConfigurationError("仓库名称、provider 和 remote 不能为空且 remote 不能以 - 开头")
        parsed = urlsplit(repo.remote)
        if parsed.scheme in {"http", "https"} and (
            parsed.username or parsed.password or parsed.query
        ):
            raise ConfigurationError("remote 不能包含认证信息或查询参数，请使用外部凭证管理")
        validate_ref(repo.base_branch)
        if repo.repository_id in identities or repo.name in names:
            raise ConfigurationError(f"重复仓库配置：{repo.name}")
        identities.add(repo.repository_id)
        names.add(repo.name)
        if repo.path:
            repo.path = _path(repo.path, base)
            if not Path(repo.path).is_dir():
                raise ConfigurationError(f"仓库路径不存在：{repo.path}")
        if not math.isfinite(repo.validation_timeout) or repo.validation_timeout <= 0:
            raise ConfigurationError("验证命令超时必须大于 0")
        if any(not command or not command[0] for command in repo.validation_commands):
            raise ConfigurationError("验证命令必须是非空参数数组")
        if repo.pipeline_enabled and config.pipeline.adapter == "disabled" and not config.dry_run:
            raise ConfigurationError("仓库启用了流水线，但流水线能力未配置")
    return config


def redacted_config(config: AppConfig) -> dict[str, Any]:
    data = asdict(config)
    # 只展示安全配置；额外 CLI 参数和验证命令可能由用户放入凭据。
    data["codex"]["extra_args"] = ["<已隐藏>"] if config.codex.extra_args else []
    for repo in data["repositories"]:
        repo["validation_commands"] = ["<已隐藏>"] if repo["validation_commands"] else []
    return data
