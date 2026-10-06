"""严格 dataclass 配置；配置文件目录决定未指定的运行目录。"""

import hashlib
import logging
import math
import os
import re
from dataclasses import asdict, dataclass, field, fields, is_dataclass
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
class AgentConfig:
    default_engine: str = "claude-cli"
    model_routes: dict[str, str] = field(default_factory=dict)
    max_concurrency: int = 4


@dataclass
class ClaudeConfig:
    binary: str = "claude"
    model: str = ""
    output_format: str = "json"
    timeout: float = 1800
    max_turns: int = 20
    allowed_tools: list[str] = field(default_factory=lambda: ["Read", "Write", "Edit"])
    permission_mode: str = "acceptEdits"
    sdk_fallback: bool = False


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
class DeclarativeConfig:
    directory: str = ""
    skills_directory: str = ""


@dataclass
class ToolConfig:
    allowed_commands: list[list[str]] = field(default_factory=list)


@dataclass
class CodeHostConfig:
    adapter: str = "logging"
    token_env: str = ""
    binary: str = "antcode"
    profile: str = ""
    timeout: float = 60


@dataclass
class PipelineConfig:
    adapter: str = "disabled"
    binary: str = "aci"
    timeout: float = 60
    poll_interval: float = 30


@dataclass
class NotificationConfig:
    adapter: str = "null"
    issue_comments: bool = False
    templates_directory: str = ""
    api_url: str = ""
    api_mode: str = "gateway"
    access_token_env: str = ""
    card_template_id: str = ""
    timeout: float = 10
    recipients: str = "assignees"
    receiver_ids: list[str] = field(default_factory=list)


@dataclass
class RepoPipelineConfig:
    project: str = ""
    yml_path: str | None = None
    template_id: str | None = None
    yaml_file: str | None = None
    yml_global_path: str | None = None
    branch: str | None = None
    params: dict[str, str] = field(default_factory=dict)
    env_file: str | None = None
    source: str = "skill"


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
    reviewers: list[str] = field(default_factory=list)
    remove_source_branch: bool = False
    pipeline: RepoPipelineConfig = field(default_factory=RepoPipelineConfig)
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
    agents: AgentConfig = field(default_factory=AgentConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
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
    declarative: DeclarativeConfig = field(default_factory=DeclarativeConfig)
    tools: ToolConfig = field(default_factory=ToolConfig)


DEFAULT_CONFIG = Path("~/.dtcoder-agentic-dev/config.yaml").expanduser()
SECTIONS = {
    "agents": AgentConfig,
    "claude": ClaudeConfig,
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
    "declarative": DeclarativeConfig,
    "tools": ToolConfig,
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
    if is_dataclass(hint):
        return isinstance(value, hint)
    if origin is dict:
        key_hint, value_hint = get_args(hint)
        return isinstance(value, dict) and all(
            _matches(k, key_hint) and _matches(v, value_hint) for k, v in value.items()
        )
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
    data = dict(data)
    for name, value in data.items():
        if is_dataclass(hints[name]):
            value = data[name] = _construct(hints[name], value)
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
    config_path = Path(_expand(str(path or DEFAULT_CONFIG))).resolve()
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
    if "agents" not in data:
        config.agents.default_engine = "codex-cli"
        logging.getLogger(__name__).warning(
            "旧配置未声明 agents，继续使用 Codex；新配置推荐 agents.default_engine: claude-cli"
        )
    engines = {"claude-cli", "claude-sdk", "codex-cli"}
    if config.agents.default_engine not in engines or any(
        engine not in engines for engine in config.agents.model_routes.values()
    ):
        raise ConfigurationError("agents 引擎必须是 claude-cli、claude-sdk 或 codex-cli")
    if config.agents.max_concurrency <= 0 or config.claude.max_turns <= 0:
        raise ConfigurationError("模型并发数和 Claude max_turns 必须大于 0")
    if not math.isfinite(config.claude.timeout) or config.claude.timeout <= 0:
        raise ConfigurationError("Claude timeout 必须是有限正数")
    if config.claude.output_format not in {"json", "stream-json"}:
        raise ConfigurationError("Claude output_format 必须是 json 或 stream-json")
    if config.claude.permission_mode not in {
        "default",
        "acceptEdits",
        "plan",
        "dontAsk",
        "bypassPermissions",
        "auto",
    }:
        raise ConfigurationError("Claude permission_mode 无效")
    if (
        not config.claude.binary.strip()
        or any(c in config.claude.binary for c in "\x00\r\n")
        or any(not tool.strip() or "\x00" in tool for tool in config.claude.allowed_tools)
    ):
        raise ConfigurationError("Claude binary 和 allowed_tools 必须是非空字符串")
    if any(not model.strip() for model in config.agents.model_routes):
        raise ConfigurationError("模型路由名称不能为空")
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
    config.declarative.directory = _path(config.declarative.directory or "workflows", base)
    config.declarative.skills_directory = _path(
        config.declarative.skills_directory or "skills", base
    )
    if any(
        not command or any(not argument or "\x00" in argument for argument in command)
        for command in config.tools.allowed_commands
    ):
        raise ConfigurationError("tools.allowed_commands 必须为非空字符串命令前缀列表")
    config.notification.templates_directory = _path(
        config.notification.templates_directory or "comments", base
    )
    positive = [
        (config.scheduler.poll_interval, "轮询间隔"),
        (config.scheduler.lease_seconds, "租约时长"),
        (config.scheduler.heartbeat_interval, "续租间隔"),
        (config.scheduler.max_nodes_per_tick, "单次最大节点数"),
        (config.codex.timeout, "Codex 超时"),
        (config.git.timeout, "Git 超时"),
        (config.code_host.timeout, "AntCode 超时"),
        (config.pipeline.timeout, "ACI 超时"),
        (config.notification.timeout, "通知超时"),
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
    if not config.code_host.binary.strip() or not config.pipeline.binary.strip():
        raise ConfigurationError("平台 CLI binary 不能为空")
    if config.notification.recipients not in {"assignees", "configured", "both"}:
        raise ConfigurationError("通知接收人策略必须是 assignees、configured 或 both")
    if config.notification.api_mode not in {"gateway", "direct"}:
        raise ConfigurationError("钉钉 api_mode 必须是 gateway 或 direct")
    if config.notification.access_token_env and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", config.notification.access_token_env
    ):
        raise ConfigurationError("钉钉 access_token_env 必须是环境变量名")
    if config.notification.adapter == "dingtalk":
        validate_url(config.notification.api_url)
        if config.notification.api_mode == "direct" and not config.notification.access_token_env:
            raise ConfigurationError("钉钉 direct 模式需要 access_token_env；认证值不得写入配置")
        if (
            not config.notification.api_url.startswith("https://")
            or not config.notification.card_template_id.strip()
        ):
            raise ConfigurationError("钉钉通知需要 HTTPS API URL 和卡片模板 ID")
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
        if (
            parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.scheme in {"http", "https"} and parsed.username)
        ):
            raise ConfigurationError("remote 不能包含认证信息或查询参数，请使用外部凭证管理")
        validate_ref(repo.base_branch)
        if repo.repository_id in identities or repo.name in names:
            raise ConfigurationError(f"重复仓库配置：{repo.name}")
        identities.add(repo.repository_id)
        names.add(repo.name)
        if repo.path:
            repo.path = _path(repo.path, base)
            source = Path(repo.path)
            for managed in (Path(config.workspace.workspaces), Path(config.workspace.mirrors)):
                if source.is_relative_to(managed) or managed.is_relative_to(source):
                    raise ConfigurationError("本地源仓库不得与受管镜像或工作区目录重叠")
            if not Path(repo.path).is_dir():
                raise ConfigurationError(f"仓库路径不存在：{repo.path}")
        if not math.isfinite(repo.validation_timeout) or repo.validation_timeout <= 0:
            raise ConfigurationError("验证命令超时必须大于 0")
        if any(not command or not command[0] for command in repo.validation_commands):
            raise ConfigurationError("验证命令必须是非空参数数组")
        repo.reviewers = list(dict.fromkeys(v.strip() for v in repo.reviewers if v.strip()))
        ci = repo.pipeline
        for name in ("yaml_file", "env_file", "yml_global_path"):
            value = getattr(ci, name)
            if value:
                setattr(ci, name, _path(value, base))
        if repo.pipeline_enabled and config.pipeline.adapter == "aci":
            if (
                not ci.project.strip()
                or sum(bool(v) for v in (ci.yml_path, ci.template_id, ci.yaml_file)) != 1
            ):
                raise ConfigurationError(
                    "ACI 必须配置 project，且 yml_path/template_id/yaml_file 恰好一种"
                )
            if any(
                v is not None and not v.strip()
                for v in (
                    ci.yml_path,
                    ci.template_id,
                    ci.yaml_file,
                    ci.yml_global_path,
                    ci.env_file,
                    ci.branch,
                )
            ):
                raise ConfigurationError("ACI 可选参数不得是空字符串")
            if ci.project.startswith("-"):
                raise ConfigurationError("ACI project 无效")
            if ci.branch:
                validate_ref(ci.branch)
            if not ci.source.strip() or any(
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", k) for k in ci.params
            ):
                raise ConfigurationError("ACI source 或参数名称无效")
            for value in ci.params.values():
                if "\x00" in value or "\n" in value:
                    raise ConfigurationError("ACI 参数值不能包含控制字符")
            if ci.yml_path and (Path(ci.yml_path).is_absolute() or ".." in Path(ci.yml_path).parts):
                raise ConfigurationError("ACI yml_path 必须位于仓库内")
            for name in ("yaml_file", "env_file", "yml_global_path"):
                value = getattr(ci, name)
                if value:
                    setattr(ci, name, _path(value, base))
                    if not Path(getattr(ci, name)).is_file():
                        raise ConfigurationError(f"ACI {name} 文件不存在")
        if config.code_host.adapter == "antcode" and not repo.project.strip():
            raise ConfigurationError("AntCode 仓库必须配置 project")
        if repo.pipeline_enabled and config.pipeline.adapter == "disabled" and not config.dry_run:
            raise ConfigurationError("仓库启用了流水线，但流水线能力未配置")
    return config


def redacted_config(config: AppConfig) -> dict[str, Any]:
    data = asdict(config)
    # 只展示安全配置；额外 CLI 参数和验证命令可能由用户放入凭据。
    data["codex"]["extra_args"] = ["<已隐藏>"] if config.codex.extra_args else []
    data["tools"]["allowed_commands"] = ["<已隐藏>"] if config.tools.allowed_commands else []
    for repo in data["repositories"]:
        repo["validation_commands"] = ["<已隐藏>"] if repo["validation_commands"] else []
    data["notification"]["api_url"] = "<已隐藏>" if config.notification.api_url else ""
    data["code_host"]["profile"] = "<已隐藏>" if config.code_host.profile else ""
    for repo in data["repositories"]:
        repo["pipeline"]["params"] = {k: "<已隐藏>" for k in repo["pipeline"]["params"]}
    from dtcoder_agentic_dev.domain.security import redact

    return redact(data)


def validate_url(value: str) -> None:
    parts = urlsplit(value)
    if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
        raise ConfigurationError("URL 不得包含认证信息、查询参数或片段")
