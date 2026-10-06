"""Codex 调用统一入口；支持纯文本和 JSON/JSONL 结构化结果。"""

import asyncio
import codecs
import json
from pathlib import Path

from dtcoder_agentic_dev.adapters.agents.cli import validate_session
from dtcoder_agentic_dev.adapters.claude.cli import session_missing
from dtcoder_agentic_dev.domain.errors import (
    AgentCancelled,
    ExternalCommandError,
    SessionLost,
    TechnicalError,
)
from dtcoder_agentic_dev.domain.security import redact, redact_text
from dtcoder_agentic_dev.infrastructure.filesystem.artifacts import contained_path
from dtcoder_agentic_dev.infrastructure.filesystem.atomic import atomic_write
from dtcoder_agentic_dev.ports.agent_executor import AgentExecutionResult


class CodexAdapter:
    engine = "codex-cli"
    supports_resume = True

    def __init__(self, config, command_runner, log_directory: str):
        self.config, self.commands = config, command_runner
        self.log_directory = str(Path(log_directory).resolve())

    def build_command(self, request=None) -> list[str]:
        command = [self.config.binary, "exec"]
        if request is not None and request.session_id:
            command.append("resume")
        command.extend(self.config.extra_args)
        if self.config.output_format == "json":
            command.append("--json")
        if request is not None and request.model:
            command.extend(["--model", request.model])
        if request is not None and request.allow_no_repo:
            command.append("--skip-git-repo-check")
        if request is not None and request.session_id:
            command.append(request.session_id)
        return [*command, "-"]

    async def execute_async(self, request):
        return await asyncio.to_thread(self.execute, request)

    def execute(self, request):
        validate_session(request.session_id)
        if request.cancel_requested and request.cancel_requested():
            raise AgentCancelled("Codex 执行已取消")
        workspace = Path(request.workspace)
        if not workspace.is_dir() or (
            not request.allow_no_repo and not (workspace / ".git").exists()
        ):
            raise TechnicalError("Codex cwd 必须是有效的隔离 Git 工作区")
        relative = f"{request.run_id}/{request.step_name}/attempt-{request.attempt_number}"
        log = contained_path(self.log_directory, relative)
        log.mkdir(parents=True, exist_ok=True)
        atomic_write(log / "prompt.txt", redact_text(request.prompt))
        try:
            kwargs = dict(
                cwd=str(workspace),
                stdin=request.prompt,
                timeout=min(self.config.timeout, request.timeout)
                if request.timeout
                else self.config.timeout,
                check=False,
            )
            if callable(getattr(type(self.commands), "run_cancellable", None)):
                decoder, buffer = codecs.getincrementaldecoder("utf-8")("replace"), ""

                def stream(data):
                    nonlocal buffer
                    buffer += decoder.decode(data)
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(event, dict) and request.on_event:
                            request.on_event(event)

                result = self.commands.run_cancellable(
                    self.build_command(request),
                    **kwargs,
                    cancel_requested=request.cancel_requested,
                    on_stdout=stream if self.config.output_format == "json" else None,
                    on_process=(
                        lambda process: request.on_event(
                            {"type": "process.started", "process": process, "engine": self.engine}
                        )
                    )
                    if request.on_event
                    else None,
                )
            else:
                result = self.commands.run(self.build_command(request), **kwargs)
        except (ExternalCommandError, AgentCancelled) as exc:
            atomic_write(log / "stdout.txt", redact_text(exc.stdout))
            atomic_write(log / "stderr.txt", redact_text(exc.stderr))
            (log / "execution.json").write_text(
                json.dumps(
                    {
                        "error_type": type(exc).__name__,
                        "error": redact_text(str(exc)),
                        "returncode": getattr(exc, "returncode", None),
                        "duration_seconds": getattr(exc, "duration_seconds", None),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            raise
        atomic_write(log / "stdout.txt", redact_text(result.stdout))
        atomic_write(log / "stderr.txt", redact_text(result.stderr))
        (log / "execution.json").write_text(
            json.dumps(
                {"returncode": result.returncode, "duration_seconds": result.duration_seconds},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        if result.returncode != 0:
            if request.session_id and session_missing(result.stdout + result.stderr):
                raise SessionLost("Codex 会话不存在；请使用 revise")
            raise ExternalCommandError(
                f"Codex 执行失败，退出码 {result.returncode}",
                returncode=result.returncode,
                stdout=redact_text(result.stdout),
                stderr=redact_text(result.stderr),
                duration_seconds=result.duration_seconds,
            )
        structured = {}
        if self.config.output_format == "json":
            try:
                try:
                    parsed = json.loads(result.stdout)
                    if not isinstance(parsed, dict):
                        raise ValueError("结果必须是对象")
                    structured = parsed
                except json.JSONDecodeError:
                    events = [
                        json.loads(line) for line in result.stdout.splitlines() if line.strip()
                    ]
                    if not events or any(not isinstance(e, dict) for e in events):
                        raise ValueError("JSONL 结果必须包含对象")
                    structured = {"events": events}
                events = structured.get("events", [structured])
                if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
                    raise ValueError("events 必须是 JSON 对象数组")
                if any(e.get("type") in {"error", "turn.failed"} for e in events):
                    raise ValueError("Codex 结构化结果报告执行失败")
            except (ValueError, TypeError) as exc:
                raise TechnicalError("Codex 结构化输出解析失败，详情见运行日志") from exc
        if request.on_event:
            for event in structured.get("events", [structured]):
                request.on_event(event)
        session = next(
            (
                event.get("thread_id")
                for event in structured.get("events", [structured])
                if event.get("type") == "thread.started"
            ),
            None,
        )
        if isinstance(session, str) and session:
            validate_session(session)
            structured["session_id"] = session
        elif request.session_id:
            structured["session_id"] = request.session_id
        return AgentExecutionResult(
            redact_text(result.stdout),
            redact_text(result.stderr),
            result.returncode,
            result.duration_seconds,
            redact(structured),
        )
