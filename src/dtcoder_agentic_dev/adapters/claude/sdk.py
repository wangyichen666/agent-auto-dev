"""可选 SDK 的流式执行；只在导入缺失且显式配置时回退到 CLI。"""

import asyncio
import importlib
import time
from dataclasses import asdict, is_dataclass, replace

from dtcoder_agentic_dev.adapters.agents.cli import save_logs, validate_session
from dtcoder_agentic_dev.adapters.claude.cli import session_missing, validate_terminal
from dtcoder_agentic_dev.domain.errors import (
    AgentCancelled,
    CapabilityNotConfigured,
    ExternalCommandTimeout,
    SessionLost,
    TechnicalError,
)
from dtcoder_agentic_dev.domain.paths import contained_path
from dtcoder_agentic_dev.domain.security import redact
from dtcoder_agentic_dev.ports.agent_executor import AgentExecutionResult


class ClaudeSDKExecutor:
    engine = "claude-sdk"
    supports_resume = True

    def __init__(self, config, log_directory, *, sdk_loader=None, fallback=None):
        self.config, self.log_directory, self.fallback = config, log_directory, fallback
        self.sdk_loader = sdk_loader or (lambda: importlib.import_module("claude_agent_sdk"))

    def execute(self, request):
        return asyncio.run(self.execute_async(request))

    async def execute_async(self, request):
        validate_session(request.session_id)
        contained_path(
            self.log_directory,
            f"{request.run_id}/{request.step_name}/attempt-{request.attempt_number}",
        )
        try:
            sdk = self.sdk_loader()
        except ImportError as exc:
            if not self.config.sdk_fallback or self.fallback is None:
                raise CapabilityNotConfigured(
                    "Claude SDK 不可用；安装 claude 可选依赖或显式启用 sdk_fallback"
                ) from exc
            adapted = replace(
                request,
                on_event=(lambda event: request.on_event({**event, "engine": "claude-cli"}))
                if request.on_event
                else None,
            )
            result = await asyncio.to_thread(self.fallback.execute, adapted)
            result.structured["fallback_reason"] = "SDK_UNAVAILABLE"
            return result
        options = sdk.ClaudeAgentOptions(
            cwd=request.workspace,
            model=request.model or self.config.model or None,
            max_turns=self.config.max_turns,
            allowed_tools=self.config.allowed_tools,
            permission_mode=self.config.permission_mode,
            resume=request.session_id,
            cli_path=self.config.binary,
        )
        started, events, terminal, event_bytes = time.monotonic(), [], None, 0
        timeout = (
            min(self.config.timeout, request.timeout) if request.timeout else self.config.timeout
        )

        async def consume():
            nonlocal terminal, event_bytes
            # query 使用当前调用独立连接；关闭迭代器会关闭其 SDK transport。
            stream = sdk.query(prompt=request.prompt, options=options)
            try:
                async for message in stream:
                    if is_dataclass(message):
                        event = asdict(message)
                    elif hasattr(message, "__dict__"):
                        event = dict(vars(message))
                    else:
                        raise TechnicalError("Claude SDK 返回未知消息类型")
                    if hasattr(message, "is_error"):
                        event["type"] = "result"
                        if terminal is not None:
                            raise TechnicalError("Claude SDK 返回重复终态")
                        terminal = event
                    else:
                        event.setdefault(
                            "type", "system" if hasattr(message, "data") else "message"
                        )
                        if isinstance(event.get("data"), dict):
                            event.setdefault("session_id", event["data"].get("session_id"))
                    event = redact(event)
                    events.append(event)
                    event_bytes += len(str(event).encode("utf-8"))
                    if len(events) > 10000 or event_bytes > 16 * 1024 * 1024:
                        raise TechnicalError("Claude SDK 事件超过大小限制")
                    if request.on_event:
                        request.on_event(event)
            finally:
                closer = getattr(stream, "aclose", None)
                if closer:
                    await closer()

        task = asyncio.create_task(consume())
        try:
            while not task.done():
                if request.cancel_requested and request.cancel_requested():
                    raise AgentCancelled("Claude SDK 执行已取消")
                if time.monotonic() - started >= timeout:
                    raise ExternalCommandTimeout("Claude SDK 执行超时")
                await asyncio.wait({task}, timeout=min(0.02, timeout))
            await task
            if terminal is None:
                raise TechnicalError("Claude SDK 流结束但没有终态")
            if request.session_id and terminal.get("is_error") and session_missing(str(terminal)):
                raise SessionLost("Claude SDK 会话不存在；请使用 revise")
            validate_terminal(terminal)
            result = AgentExecutionResult(
                redact(str(terminal.get("result") or "")),
                duration_seconds=time.monotonic() - started,
                structured=redact({**terminal, "engine": self.engine}),
            )
            save_logs(
                self.log_directory,
                request,
                stdout=result.stdout,
                metadata={**result.structured, "events": events},
            )
            return result
        except Exception as exc:
            if (
                request.session_id
                and not isinstance(exc, (AgentCancelled, ExternalCommandTimeout, SessionLost))
                and session_missing(str(exc))
            ):
                exc = SessionLost("Claude SDK 会话不存在；请使用 revise")
            metadata = {
                "engine": self.engine,
                "session_id": (terminal or {}).get("session_id", request.session_id),
                "error_code": getattr(exc, "code", "SDK_FAILED"),
                "error": redact(str(exc)),
                "events": events,
            }
            exc.execution_metadata = metadata
            try:
                save_logs(self.log_directory, request, metadata=metadata)
            except OSError:
                pass
            if isinstance(
                exc, (AgentCancelled, ExternalCommandTimeout, SessionLost, TechnicalError)
            ):
                raise exc
            wrapped = TechnicalError("Claude SDK 执行失败，详情见脱敏日志")
            wrapped.execution_metadata = metadata
            raise wrapped from exc
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
