"""执行器路由及持久化桥接，共享当前工作流租约。"""

from dataclasses import replace

from dtcoder_agentic_dev.domain.errors import (
    AgentCancelled,
    ConcurrencyConflict,
    ConfigurationError,
    ExternalCommandTimeout,
    SessionLost,
    TechnicalError,
)
from dtcoder_agentic_dev.domain.models import AttemptStatus, RunStatus
from dtcoder_agentic_dev.domain.security import redact
from dtcoder_agentic_dev.ports.agent_executor import AgentExecutionStatus


class AgentRouter:
    engine = "default"

    def __init__(self, config, executors):
        self.config, self.executors = config, executors
        self.resolve(config.default_engine)
        for engine in config.model_routes.values():
            self.resolve(engine)

    def engine_for(self, model=None):
        return self.config.model_routes.get(model, self.config.default_engine)

    def resolve(self, engine):
        try:
            return self.executors[engine]
        except KeyError as exc:
            raise ConfigurationError(f"未知模型引擎：{engine}") from exc

    def execute(self, request):
        return self.resolve(self.engine_for(request.model)).execute(request)


class ManagedAgentExecutor:
    """不另建任务数据库；每个 session 事件写入当前 RUNNING attempt。"""

    def __init__(self, executor, manager, store, clock):
        self.executor, self.manager, self.store, self.clock = executor, manager, store, clock
        self.engine = executor.engine
        self.supports_resume = getattr(executor, "supports_resume", False)
        self.default_model = getattr(getattr(executor, "config", None), "model", None) or None

    def execute(self, request):
        initial = self.store.load_run(request.run_id)
        owner = initial.lease_owner
        with self.store.transaction():
            self.store.assert_lease(request.run_id, owner, self.clock.now())
            attempt = next(
                a
                for a in self.store.list_attempts(request.run_id)
                if a.step_name == request.step_name and a.attempt_number == request.attempt_number
            )
            if attempt.status is not AttemptStatus.RUNNING:
                raise ConcurrencyConflict("不能执行已终止 attempt")
            attempt.metadata.update(engine=self.engine, model=request.model)
            if request.session_id:
                attempt.metadata["session_id"] = request.session_id
            self.store.save_attempt(attempt)

        def cancelled():
            current = self.store.load_run(request.run_id)
            if current.lease_owner != owner or (current.lease_expires_at or 0) <= self.clock.now():
                return True
            return current.status in {RunStatus.PAUSED, RunStatus.CANCELLED} or bool(
                request.cancel_requested and request.cancel_requested()
            )

        def event_received(event):
            session = event.get("session_id") or event.get("thread_id")
            if session or event.get("process"):
                with self.store.transaction():
                    self.store.assert_lease(request.run_id, owner, self.clock.now())
                    attempts = self.store.list_attempts(request.run_id)
                    attempt = next(
                        a
                        for a in attempts
                        if a.step_name == request.step_name
                        and a.attempt_number == request.attempt_number
                    )
                    if attempt.status is not AttemptStatus.RUNNING:
                        raise ConcurrencyConflict("session 事件不能修改已终止 attempt")
                    attempt.metadata.update(
                        engine=event.get("engine", self.engine), model=request.model
                    )
                    if session:
                        attempt.metadata["session_id"] = session
                    if event.get("process"):
                        attempt.metadata["process"] = event["process"]
                    self.store.save_attempt(attempt)
            if request.on_event:
                request.on_event(redact(event))

        adapted = replace(request, cancel_requested=cancelled, on_event=event_received)
        try:
            result = self.manager.submit(self.executor, adapted).result()
        finally:
            self.manager.forget(adapted)
        # manager 输出的错误保持原分类；取消/超时绝不被当作成功。
        if result.status is not AgentExecutionStatus.SUCCEEDED:
            code = result.structured.get("error_code")
            cls = (
                AgentCancelled
                if result.status is AgentExecutionStatus.CANCELLED
                else ExternalCommandTimeout
                if result.status is AgentExecutionStatus.TIMED_OUT
                else SessionLost
                if code == "SESSION_LOST"
                else ConcurrencyConflict
                if code == "CONCURRENCY_CONFLICT"
                else TechnicalError
            )
            exc = cls(result.structured.get("error", "模型执行失败"))
            if code:
                exc.code = code
            exc.execution_metadata = {
                **result.structured,
                "engine": result.structured.get("engine", self.engine),
                "stdout": result.stdout,
                "stderr": result.stderr,
                "duration_seconds": result.duration_seconds,
            }
            raise exc
        return result
