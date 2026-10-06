"""异步运行句柄管理。持久化事实由调用方的 attempt/租约负责。"""

import asyncio
import threading
import time
from dataclasses import replace

from dtcoder_agentic_dev.domain.errors import (
    AgentCancelled,
    ConfigurationError,
    ExternalCommandTimeout,
)
from dtcoder_agentic_dev.domain.security import redact
from dtcoder_agentic_dev.ports.agent_executor import AgentExecutionResult, AgentExecutionStatus


class AgentManager:
    def __init__(self, max_concurrency=4):
        if type(max_concurrency) is not int or max_concurrency <= 0:
            raise ConfigurationError("模型并发数必须是正整数")
        self.limit, self.running = max_concurrency, False
        self._lock, self._handles = threading.RLock(), {}

    def start(self):
        with self._lock:
            if self.running:
                return
            ready = threading.Event()

            def serve():
                self.loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self.loop)
                self.semaphore = asyncio.Semaphore(self.limit)
                ready.set()
                self.loop.run_forever()
                self.loop.run_until_complete(self.loop.shutdown_asyncgens())
                self.loop.run_until_complete(self.loop.shutdown_default_executor())
                self.loop.close()

            self.thread = threading.Thread(target=serve, name="dtcoder-agents", daemon=True)
            self.thread.start()
            ready.wait()
            self.running = True

    def submit(self, executor, request):
        with self._lock:
            if not self.running:
                raise ConfigurationError("模型 manager 尚未启动")
            key = (request.run_id, request.step_name, request.attempt_number)
            if key in self._handles:
                raise ConfigurationError("相同 attempt 已提交；请读取原句柄")
            cancel = threading.Event()
            original = request.cancel_requested
            request = replace(
                request, cancel_requested=lambda: cancel.is_set() or bool(original and original())
            )
            future = asyncio.run_coroutine_threadsafe(
                self._execute(executor, request, cancel), self.loop
            )
            self._handles[key] = (future, cancel)
            return future

    async def _execute(self, executor, request, cancellation):
        started, timed_out = time.monotonic(), False
        try:
            async with self.semaphore:
                if request.timeout and time.monotonic() - started >= request.timeout:
                    raise ExternalCommandTimeout("等待模型执行容量超时")
                if request.cancel_requested():
                    raise AgentCancelled("模型执行已取消")
                async_execute = getattr(executor, "execute_async", None)
                task = asyncio.create_task(
                    async_execute(request)
                    if async_execute
                    else asyncio.to_thread(executor.execute, request)
                )
                try:
                    while not task.done():
                        if request.timeout and time.monotonic() - started >= request.timeout:
                            timed_out = True
                            cancellation.set()
                        await asyncio.wait({task}, timeout=0.02)
                    result = await task
                    if not isinstance(result, AgentExecutionResult):
                        raise TypeError("模型执行器必须返回 AgentExecutionResult")
                    if timed_out:
                        raise ExternalCommandTimeout("模型执行超时")
                    if result.returncode or result.status is not AgentExecutionStatus.SUCCEEDED:
                        return replace(
                            result,
                            status=result.status
                            if result.status is not AgentExecutionStatus.SUCCEEDED
                            else AgentExecutionStatus.FAILED,
                            stdout=redact(result.stdout),
                            stderr=redact(result.stderr),
                            structured=redact(result.structured),
                        )
                    return replace(
                        result,
                        stdout=redact(result.stdout),
                        stderr=redact(result.stderr),
                        structured=redact(result.structured),
                    )
                finally:
                    # 不取消 to_thread future 后提前释放工作流租约，必须等待执行器回收拥有的进程。
                    if not task.done():
                        cancellation.set()
                        await asyncio.gather(task, return_exceptions=True)
        except Exception as exc:
            status = (
                AgentExecutionStatus.TIMED_OUT
                if timed_out or isinstance(exc, ExternalCommandTimeout)
                else AgentExecutionStatus.CANCELLED
                if isinstance(exc, AgentCancelled)
                else AgentExecutionStatus.FAILED
            )
            metadata = {
                **getattr(exc, "execution_metadata", {}),
                "error_code": "TIMEOUT"
                if status is AgentExecutionStatus.TIMED_OUT
                else getattr(exc, "code", "EXECUTION_FAILED"),
                "error": redact(str(exc)),
                "error_type": type(exc).__name__,
            }
            return AgentExecutionResult(
                redact(getattr(exc, "stdout", "")),
                redact(getattr(exc, "stderr", "")),
                returncode=1,
                duration_seconds=time.monotonic() - started,
                structured=redact(metadata),
                status=status,
            )

    def cancel(self, run_id):
        with self._lock:
            for key, (future, event) in self._handles.items():
                if key[0] == run_id and not future.done():
                    event.set()

    def status(self, run_id, step_name, attempt_number):
        with self._lock:
            future, _ = self._handles[(run_id, step_name, attempt_number)]
            return future.result().status.value if future.done() else "RUNNING"

    def forget(self, request):
        with self._lock:
            key = (request.run_id, request.step_name, request.attempt_number)
            handle = self._handles.get(key)
            if handle and handle[0].done():
                del self._handles[key]

    def stop(self):
        with self._lock:
            if not self.running:
                return
            self.running = False
            handles = list(self._handles.values())
            for _, event in handles:
                event.set()
        for future, _ in handles:
            future.result()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()
        with self._lock:
            self._handles.clear()
