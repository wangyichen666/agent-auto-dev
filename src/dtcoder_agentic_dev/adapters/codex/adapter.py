"""Codex 调用统一入口；支持纯文本和 JSON/JSONL 结构化结果。"""

import json
from pathlib import Path

from dtcoder_agentic_dev.domain.errors import ExternalCommandError, TechnicalError
from dtcoder_agentic_dev.infrastructure.filesystem.artifacts import contained_path
from dtcoder_agentic_dev.ports.agent_executor import AgentExecutionResult


class CodexAdapter:
    def __init__(self, config, command_runner, log_directory: str):
        self.config, self.commands = config, command_runner
        self.log_directory = str(Path(log_directory).resolve())

    def build_command(self) -> list[str]:
        command = [self.config.binary, "exec", *self.config.extra_args]
        if self.config.output_format == "json":
            command.append("--json")
        return [*command, "-"]

    def execute(self, request):
        workspace = Path(request.workspace)
        if not workspace.is_dir() or not (workspace / ".git").exists():
            raise TechnicalError("Codex cwd 必须是有效的隔离 Git 工作区")
        relative = f"{request.run_id}/{request.step_name}/attempt-{request.attempt_number}"
        log = contained_path(self.log_directory, relative)
        log.mkdir(parents=True, exist_ok=True)
        (log / "prompt.txt").write_text(request.prompt, encoding="utf-8")
        try:
            result = self.commands.run(
                self.build_command(),
                cwd=str(workspace),
                stdin=request.prompt,
                timeout=self.config.timeout,
                check=False,
            )
        except ExternalCommandError as exc:
            (log / "stdout.txt").write_text(exc.stdout, encoding="utf-8")
            (log / "stderr.txt").write_text(exc.stderr, encoding="utf-8")
            (log / "execution.json").write_text(
                json.dumps(
                    {
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "returncode": exc.returncode,
                        "duration_seconds": exc.duration_seconds,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            raise
        (log / "stdout.txt").write_text(result.stdout, encoding="utf-8")
        (log / "stderr.txt").write_text(result.stderr, encoding="utf-8")
        (log / "execution.json").write_text(
            json.dumps(
                {"returncode": result.returncode, "duration_seconds": result.duration_seconds},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        if result.returncode != 0:
            raise ExternalCommandError(
                f"Codex 执行失败，退出码 {result.returncode}", returncode=result.returncode
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
        return AgentExecutionResult(
            result.stdout, result.stderr, result.returncode, result.duration_seconds, structured
        )
