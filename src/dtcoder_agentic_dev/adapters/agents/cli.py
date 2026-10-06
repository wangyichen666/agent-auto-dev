"""通用本地 CLI；提示词经 stdin，参数不插值用户输入。"""

import asyncio
import json
import re
from pathlib import Path

from dtcoder_agentic_dev.domain.errors import (
    AgentCancelled,
    ConfigurationError,
    ExternalCommandError,
)
from dtcoder_agentic_dev.domain.security import redact
from dtcoder_agentic_dev.infrastructure.filesystem.artifacts import contained_path
from dtcoder_agentic_dev.infrastructure.filesystem.atomic import atomic_write
from dtcoder_agentic_dev.ports.agent_executor import AgentExecutionResult


def validate_session(session):
    if session is not None and (
        not isinstance(session, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}", session)
    ):
        raise ConfigurationError("session ID 必须是非空安全标识符")


def save_logs(root, request, *, stdout="", stderr="", metadata=None):
    path = contained_path(
        root, f"{request.run_id}/{request.step_name}/attempt-{request.attempt_number}"
    )
    path.mkdir(parents=True, exist_ok=True)
    for name, content in [
        ("prompt.txt", request.prompt),
        ("stdout.txt", stdout),
        ("stderr.txt", stderr),
    ]:
        atomic_write(path / name, redact(content))
    atomic_write(path / "execution.json", json.dumps(redact(metadata or {}), ensure_ascii=False))


class GenericCLIExecutor:
    engine = "generic-cli"
    supports_resume = False

    def __init__(self, command, commands, log_directory, *, timeout=1800):
        if (
            not isinstance(command, (list, tuple))
            or not command
            or any(not isinstance(a, str) or not a or "\x00" in a for a in command)
        ):
            raise ConfigurationError("通用 CLI command 必须是非空参数数组")
        self.command, self.commands, self.log_directory, self.timeout = (
            list(command),
            commands,
            log_directory,
            timeout,
        )

    def build_command(self, request):
        if request.session_id:
            raise ConfigurationError("通用 CLI 未声明 session resume 协议")
        return self.command

    def parse_result(self, result, request):
        if result.returncode:
            raise ExternalCommandError(
                "模型命令失败",
                returncode=result.returncode,
                stdout=redact(result.stdout),
                stderr=redact(result.stderr),
            )
        return {"engine": self.engine}

    def stream_handler(self, request):
        return None

    def execute(self, request):
        validate_session(request.session_id)
        if not Path(request.workspace).is_dir():
            raise ConfigurationError("模型工作目录不存在")
        if request.cancel_requested and request.cancel_requested():
            raise AgentCancelled("模型执行已取消")
        contained_path(
            self.log_directory,
            f"{request.run_id}/{request.step_name}/attempt-{request.attempt_number}",
        )
        result = None
        try:
            command = self.build_command(request)
            kwargs = dict(
                cwd=request.workspace,
                stdin=request.prompt,
                timeout=min(self.timeout, request.timeout) if request.timeout else self.timeout,
                check=False,
            )
            if callable(getattr(type(self.commands), "run_cancellable", None)):
                result = self.commands.run_cancellable(
                    command,
                    **kwargs,
                    cancel_requested=request.cancel_requested,
                    on_stdout=self.stream_handler(request),
                    on_process=(
                        lambda process: request.on_event(
                            {"type": "process.started", "process": process, "engine": self.engine}
                        )
                    )
                    if request.on_event
                    else None,
                )
            else:
                result = self.commands.run(command, **kwargs)
            structured = self.parse_result(result, request)
            save_logs(
                self.log_directory,
                request,
                stdout=result.stdout,
                stderr=result.stderr,
                metadata={
                    **structured,
                    "returncode": result.returncode,
                    "duration_seconds": result.duration_seconds,
                },
            )
            return AgentExecutionResult(
                redact(result.stdout),
                redact(result.stderr),
                result.returncode,
                result.duration_seconds,
                redact(structured),
            )
        except Exception as exc:
            metadata = {
                "engine": self.engine,
                "session_id": request.session_id,
                "error_code": getattr(exc, "code", "EXECUTION_FAILED"),
                "error": redact(str(exc)),
            }
            stdout = result.stdout if result else getattr(exc, "stdout", "")
            stderr = result.stderr if result else getattr(exc, "stderr", "")
            exc.execution_metadata = {
                **metadata,
                "stdout": redact(stdout),
                "stderr": redact(stderr),
            }
            try:
                save_logs(
                    self.log_directory, request, stdout=stdout, stderr=stderr, metadata=metadata
                )
            except OSError:
                pass
            raise

    async def execute_async(self, request):
        return await asyncio.to_thread(self.execute, request)
