"""Claude Code 的非交互结构化执行；不读取认证数据。"""

import codecs

from dtcoder_agentic_dev.adapters.agents.cli import GenericCLIExecutor, validate_session
from dtcoder_agentic_dev.domain.errors import (
    AgentCancelled,
    ExternalCommandError,
    SessionLost,
    TechnicalError,
)
from dtcoder_agentic_dev.domain.json import strict_json_loads
from dtcoder_agentic_dev.domain.security import redact


def session_missing(text):
    # 仅把 CLI 明确的会话不存在诊断映射为 SESSION_LOST，不猜测通用故障。
    return any(
        marker in text.lower()
        for marker in (
            "no conversation found with session",
            "session not found",
            "session does not exist",
        )
    )


def validate_terminal(event):
    reason = event.get("terminal_reason")
    if reason in {"aborted_tools", "aborted_streaming"}:
        raise AgentCancelled("Claude 会话执行已中断")
    if reason is not None and reason != "completed":
        raise TechnicalError("Claude 未正常完成：" + redact(str(reason)))
    if (
        event.get("type") != "result"
        or type(event.get("is_error")) is not bool
        or not isinstance(event.get("subtype"), str)
    ):
        raise TechnicalError("Claude 缺少合法的结构化终态")
    if event["is_error"] or event["subtype"] != "success":
        raise TechnicalError("Claude 报告执行失败：" + redact(str(event.get("subtype"))))
    session = event.get("session_id")
    validate_session(session)
    if not session:
        raise TechnicalError("Claude 终态缺少 session_id")
    return event


class ClaudeCLIExecutor(GenericCLIExecutor):
    engine = "claude-cli"
    supports_resume = True

    def __init__(self, config, commands, log_directory):
        super().__init__([config.binary], commands, log_directory, timeout=config.timeout)
        self.config = config

    def build_command(self, request):
        command = [
            self.config.binary,
            "--print",
            "--output-format",
            self.config.output_format,
            "--max-turns",
            str(self.config.max_turns),
            "--permission-mode",
            self.config.permission_mode,
        ]
        if self.config.output_format == "stream-json":
            command.append("--verbose")
        if self.config.allowed_tools:
            command.extend(["--allowedTools", *self.config.allowed_tools])
        if request.model or self.config.model:
            command.extend(["--model", request.model or self.config.model])
        if request.session_id:
            command.extend(["--resume", request.session_id])
        return command

    def stream_handler(self, request):
        if not request.on_event or self.config.output_format != "stream-json":
            return None
        decoder, buffer = codecs.getincrementaldecoder("utf-8")("replace"), ""

        def consume(data):
            nonlocal buffer
            buffer += decoder.decode(data)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if line.strip():
                    try:
                        event = strict_json_loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        request.on_event(redact(event))

        return consume

    def parse_result(self, result, request):
        if result.returncode:
            if request.session_id and session_missing(result.stderr + result.stdout):
                raise SessionLost("Claude 会话不存在；请使用 revise 显式重新执行")
            raise ExternalCommandError(
                "Claude 执行失败",
                returncode=result.returncode,
                stdout=redact(result.stdout),
                stderr=redact(result.stderr),
            )
        try:
            if self.config.output_format == "json":
                events = [strict_json_loads(result.stdout)]
            else:
                events = [
                    strict_json_loads(line) for line in result.stdout.splitlines() if line.strip()
                ]
            if not events or any(not isinstance(e, dict) for e in events):
                raise ValueError
        except ValueError as exc:
            raise TechnicalError("Claude 结构化输出解析失败，详情见运行日志") from exc
        if request.on_event:
            for event in events:
                request.on_event(redact(event))
        if request.session_id and any(session_missing(str(e)) for e in events if e.get("is_error")):
            raise SessionLost("Claude 会话不存在；请使用 revise 显式重新执行")
        result_events = [e for e in events if e.get("type") == "result"]
        if len(result_events) != 1 or result_events[0] is not events[-1]:
            raise TechnicalError("Claude 必须返回唯一的最终 result 事件")
        final = validate_terminal(result_events[0])
        return {**final, "engine": self.engine, "events": events}
