"""本地工具只操作任务工作区；命令须匹配运维配置的参数前缀。"""

from dataclasses import dataclass
from pathlib import Path

from dtcoder_agentic_dev.domain.errors import (
    CapabilityNotConfigured,
    ConfigurationError,
    ExternalCommandError,
    TechnicalError,
)
from dtcoder_agentic_dev.domain.paths import artifact_path
from dtcoder_agentic_dev.domain.security import redact_text
from dtcoder_agentic_dev.infrastructure.filesystem.atomic import atomic_write
from dtcoder_agentic_dev.ports.tool_executor import ToolResult


@dataclass
class LocalTool:
    owner: object
    name: str

    def file_path(self, workspace, relative):
        return artifact_path(workspace, relative)

    def requires_repository(self, config):
        command = config.get("command", [])
        return bool(
            command
            and Path(command[0]).name == "git"
            and any(
                arg
                in {
                    "commit",
                    "push",
                    "reset",
                    "clean",
                    "checkout",
                    "switch",
                    "add",
                    "merge",
                    "rebase",
                    "worktree",
                }
                for arg in command[1:]
            )
        )

    def execute(self, request):
        config = request.config
        if self.name == "file.write":
            target = self.file_path(request.workspace, config["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(target, config["content"])
            return ToolResult(metadata={"path": config["path"]})
        if self.name == "file.copy":
            source = self.file_path(request.workspace, config["source"])
            target = self.file_path(request.workspace, config["target"])
            try:
                content = source.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise TechnicalError("源文件不存在或不是 UTF-8 文本") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(target, content)
            return ToolResult(metadata={"path": config["target"]})
        args = config["command"]
        if self.requires_repository(config):
            raise CapabilityNotConfigured("Git 写操作必须通过专用受管 Git Port，本地命令工具不执行")
        if not any(args[: len(prefix)] == list(prefix) for prefix in self.owner.allowed_commands):
            raise CapabilityNotConfigured("命令不在 tools.allowed_commands 参数前缀白名单中")
        if self.owner.commands is None:
            raise CapabilityNotConfigured("未装配本地 CommandRunner")
        result = self.owner.commands.run(
            args, cwd=request.workspace, timeout=request.timeout, check=True
        )
        if result.returncode != 0:
            raise ExternalCommandError(
                "本地工具执行失败",
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_seconds=result.duration_seconds,
            )
        return ToolResult(
            redact_text(result.stdout), redact_text(result.stderr), result.duration_seconds
        )


class LocalTools:
    def __init__(self, commands, allowed_commands=()):
        self.commands, self.allowed_commands = commands, tuple(tuple(p) for p in allowed_commands)
        if any(
            not p or any(not isinstance(a, str) or not a for a in p) for p in self.allowed_commands
        ):
            raise ConfigurationError("工具白名单必须为非空命令参数前缀列表")

    def register(self, registry):
        for name in ("file.write", "file.copy", "command", "test"):
            registry.register_tool(name, LocalTool(self, name))
