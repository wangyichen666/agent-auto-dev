"""YAML 任务用例。控制操作与调度共用 SQLite/CAS/租约，历史永不覆盖。"""

from dataclasses import replace

from dtcoder_agentic_dev.application.yaml_workflows import (
    compile_workflow,
    parse_workflow,
    resolved_yaml,
)
from dtcoder_agentic_dev.domain.errors import ConcurrencyConflict, ConfigurationError
from dtcoder_agentic_dev.domain.events import EventType
from dtcoder_agentic_dev.domain.models import (
    ACTIVE_STATUSES,
    AttemptStatus,
    Issue,
    RunStatus,
    WorkflowRun,
)
from dtcoder_agentic_dev.domain.security import redact, redact_text
from dtcoder_agentic_dev.engine.runner import WorkflowRunner


class DeclarativeRunService:
    def __init__(
        self,
        store,
        definitions,
        executors,
        services,
        events,
        ids,
        clock,
        workspaces,
        sleeper,
        *,
        max_nodes=20,
        head_reader=None,
        local_workspace=None,
        stop_requested=None,
        process_is_alive=None,
    ):
        self.store, self.definitions, self.executors, self.services = (
            store,
            definitions,
            executors,
            services,
        )
        self.events, self.ids, self.clock, self.workspaces, self.sleeper = (
            events,
            ids,
            clock,
            workspaces,
            sleeper,
        )
        self.max_nodes, self.head_reader = max_nodes, head_reader
        self.local_workspace = local_workspace
        self.stop_requested = stop_requested or (lambda: False)
        self.process_is_alive = process_is_alive

    def parse(self, source):
        return parse_workflow(
            source, agents=self.executors.agent_names, tools=self.executors.tool_names
        )

    def submit(self, source, task, repo=None):
        if not isinstance(task, str) or not task.strip():
            raise ConfigurationError("任务需求不能为空")
        definition = self.parse(source)
        # skill 在创建时解析为内联 prompt，模板之后的修改不影响历史任务。
        definition = replace(
            definition,
            jobs=tuple(
                replace(job, prompt=self.definitions.read_skill(job.skill), skill=None)
                if job.skill
                else job
                for job in definition.jobs
            ),
        )
        # default 的选择在提交时冻结，之后的配置改变不会重新选择历史引擎。
        definition = replace(
            definition,
            jobs=tuple(
                replace(job, agent=self.executors.resolve_agent("default").engine_for(job.model))
                if job.type == "agent"
                and job.agent == "default"
                and hasattr(self.executors.resolve_agent("default"), "engine_for")
                else job
                for job in definition.jobs
            ),
        )
        definition = replace(
            definition,
            jobs=tuple(
                replace(
                    job,
                    model=getattr(self.executors.resolve_agent(job.agent), "default_model", None),
                )
                if job.type == "agent" and job.model is None
                else job
                for job in definition.jobs
            ),
        )
        if self.services.prompt_builder is not None:
            definition = replace(
                definition,
                metadata={
                    **definition.metadata,
                    "prompt_template": self.services.prompt_builder.freeze(
                        definition.context.get("language", "zh")
                    ),
                },
            )
        compile_workflow(definition, self.executors, repo_available=repo is not None)
        resolved = resolved_yaml(definition)
        self.parse(resolved)
        run_id, now = self.ids.new(), self.clock.now()
        reference = self.definitions.save(run_id, source, resolved)
        issue = Issue(
            "local-task",
            repo.repository_id if repo else "",
            run_id,
            0,
            redact_text(task.splitlines()[0]),
            body=redact_text(task),
        )
        run = WorkflowRun(
            run_id,
            definition.name,
            definition.version,
            issue.repository_id,
            issue.external_id,
            0,
            current_step=definition.ordered_jobs[0].name,
            branch=f"agent/task-{run_id}" if repo else "",
            base_branch=repo.base_branch if repo else "",
            context={
                "workflow_definition": reference,
                "definition_schema_version": 1,
                "declarative_epoch": 0,
                "stage_executions": [],
                "repo_mode": "single" if repo else "none",
            },
            created_at=now,
            updated_at=now,
        )
        with self.store.transaction():
            self.store.save_issue(issue)
            self.store.create_run(run)
            event = self.events.make(EventType.RUN_QUEUED, run_id)
            self.store.save_event(event)
        self.events.publish([event])
        return run

    def _live_process(self, run_id):
        if self.process_is_alive is None:
            return None
        for attempt in self.store.list_attempts(run_id):
            record = attempt.metadata.get("process")
            if record and self.process_is_alive(record):
                return attempt
        return None

    def runner_for(self, run):
        orphan = self._live_process(run.run_id)
        if orphan is not None:
            with self.store.transaction():
                self.store.assert_lease(run.run_id, run.lease_owner, self.clock.now())
                current = self.store.load_run(run.run_id)
                if current.status in {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING}:
                    current.status = RunStatus.PAUSED
                    current.context["pause_point"] = {
                        "job": orphan.step_name,
                        "stage": orphan.metadata.get("stage"),
                        "round": orphan.metadata.get("round", 1),
                        "reason": "orphan_process_active",
                        "session_id": orphan.metadata.get("session_id"),
                        "attempt_id": orphan.attempt_id,
                        "created_at": self.clock.now(),
                        "revision": current.revision,
                    }
                    for stage in current.context.get("stage_executions", []):
                        if stage["execution_id"] == orphan.metadata.get("stage_execution_id"):
                            stage.update(status="PAUSED", finished_at=self.clock.now())
                    if orphan.status is AttemptStatus.RUNNING:
                        orphan.status = AttemptStatus.PAUSED
                        orphan.finished_at = self.clock.now()
                        self.store.save_attempt(orphan)
                    self.store.update_run(current)
                    event = self.events.make(
                        EventType.RUN_PAUSED,
                        run.run_id,
                        payload={"reason": "orphan_process_active"},
                    )
                    self.store.save_event(event)
                else:
                    event = None
            if event:
                self.events.publish([event])
        _, resolved = self.definitions.read(run.run_id, run.context["workflow_definition"])
        definition = self.parse(resolved)
        graph, registry = compile_workflow(
            definition, self.executors, repo_available=bool(run.repository_id)
        )
        return WorkflowRunner(
            self.store,
            registry,
            graph,
            self.services,
            self.events,
            self.ids,
            self.sleeper,
            max_nodes_per_tick=self.max_nodes,
            head_reader=self.head_reader if run.repository_id else None,
            stop_requested=self.stop_requested,
        )

    def prepare_workspace(self, run, repo, workspace_manager=None):
        if bool(run.repository_id) != (repo is not None) or (
            repo is not None and repo.repository_id != run.repository_id
        ):
            raise ConfigurationError("运行仓库身份与工作区请求不一致")
        if repo is not None:
            if workspace_manager is None:
                raise ConfigurationError("单仓任务未装配 worktree manager")
            path = (
                workspace_manager.recover(run, repo)
                if run.workspace_path
                else workspace_manager.prepare(run, repo)
            )
        else:
            if self.local_workspace is None:
                raise ConfigurationError("无仓任务未装配受管工作区 Port")
            path = self.local_workspace.prepare(run)
        with self.store.transaction():
            self.store.assert_lease(run.run_id, run.lease_owner, self.clock.now())
            current = self.store.load_run(run.run_id)
            current.workspace_path = path
            if run.context.get("base_revision"):
                current.context.setdefault("base_revision", run.context["base_revision"])
            current.updated_at = self.clock.now()
            self.store.update_run(current)
        return current

    def control(self, run_id, action, *, feedback=None, mode="revise"):
        if mode not in {"revise", "continue_conversation"}:
            raise ConfigurationError("恢复方式必须是 revise 或 continue_conversation")
        if mode == "continue_conversation" and action not in {"resume", "retry"}:
            raise ConfigurationError("continue_conversation 只适用于 resume/retry")
        event = None
        with self.store.transaction():
            run = self.store.load_run(run_id)
            if not run.context.get("workflow_definition"):
                raise ConfigurationError("此用例仅处理 YAML 任务")
            allowed = {
                "pause": {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING},
                "resume": {RunStatus.PAUSED},
                "cancel": ACTIVE_STATUSES,
                "retry": {RunStatus.FAILED, RunStatus.CANCELLED},
                "skip": {RunStatus.PAUSED},
            }
            if action not in allowed or run.status not in allowed[action]:
                raise ConfigurationError(f"{run.status.value} 状态不能执行 {action}")
            point = run.context.get("pause_point")
            if action in {"resume", "retry"}:
                _, resolved = self.definitions.read(run_id, run.context["workflow_definition"])
                definition = self.parse(resolved)
                job = next((job for job in definition.jobs if job.name == run.current_step), None)
                if mode == "continue_conversation":
                    if (point or {}).get("reason") == "recovery_requires_review":
                        raise ConfigurationError("中断会话未通过恢复身份校验；请使用 revise")
                    if (
                        job is None
                        or job.type != "agent"
                        or not getattr(
                            self.executors.resolve_agent(job.agent), "supports_resume", False
                        )
                    ):
                        raise ConfigurationError("当前节点没有可续聊的 agent 会话运行时")
                    candidates = [
                        a for a in self.store.list_attempts(run_id) if a.step_name == job.name
                    ]
                    attempt = candidates[-1] if candidates else None
                    session = (point or {}).get("session_id") or (
                        attempt.metadata.get("session_id") if attempt else None
                    )
                    engine = attempt.metadata.get("engine") if attempt else None
                    if not session or engine != getattr(
                        self.executors.resolve_agent(job.agent), "engine", job.agent
                    ):
                        raise ConfigurationError("当前节点缺少匹配的 session；请使用 revise")
                    run.context["agent_resume"] = {
                        "job": job.name,
                        "session_id": session,
                        "engine": engine,
                        "mode": mode,
                    }
                else:
                    run.context.pop("agent_resume", None)
            if action in {"resume", "retry", "skip"}:
                if self._live_process(run_id) is not None:
                    raise ConcurrencyConflict("历史 agent 进程组仍存活，禁止启动另一执行")
                if run.lease_owner and (run.lease_expires_at or 0) > self.clock.now():
                    raise ConcurrencyConflict("当前原子动作仍持有租约，请等待安全边界")
                if action == "skip":
                    _, resolved = self.definitions.read(run_id, run.context["workflow_definition"])
                    definition = self.parse(resolved)
                    job = next(
                        (job for job in definition.jobs if job.name == run.current_step), None
                    )
                    if job is None or not job.policy.allow_skip:
                        raise ConfigurationError("当前 job 未声明 allow_skip，不能跳过")
                    loop = next((loop for loop in definition.loops if job.name in loop.jobs), None)
                    round_number = (
                        run.context.get("loop_rounds", {}).get(loop.name, 0) + 1 if loop else 1
                    )
                    run.context.setdefault("skip_jobs", []).append(f"{job.name}:{round_number}")
                if feedback is not None:
                    run.context.setdefault("feedback", []).append(
                        {
                            "action": action,
                            "mode": mode,
                            "revision": run.revision,
                            "created_at": self.clock.now(),
                            "stage": (point or {}).get("stage"),
                            "job": (point or {}).get("job", run.current_step),
                            "session_id": (point or {}).get("session_id"),
                            "text": redact(feedback),
                        }
                    )
                if point:
                    run.context.setdefault("pause_history", []).append(
                        {**point, "action": action, "revision": run.revision}
                    )
                    if point["reason"] in {"approval", "human"}:
                        run.context.setdefault("approvals", []).append(
                            f"{point['job']}:{point['round']}"
                        )
                    elif point["reason"] == "loop_exhausted":
                        _, resolved = self.definitions.read(
                            run_id, run.context["workflow_definition"]
                        )
                        loop = next(
                            loop_entry
                            for loop_entry in self.parse(resolved).loops
                            if loop_entry.name == point["loop"]
                        )
                        run.current_step = loop.jobs[0]
                        run.context.setdefault("loop_rounds", {})[loop.name] = 0
                        run.context["declarative_epoch"] += 1
                        run.context["approvals"] = []
                        run.context["skip_jobs"] = []
                    run.context.pop("pause_point", None)
                if (
                    action == "retry"
                    and run.context.get("fail_point", {}).get("error_code") == "LOOP_EXHAUSTED"
                ):
                    _, resolved = self.definitions.read(run_id, run.context["workflow_definition"])
                    loop = next(
                        loop for loop in self.parse(resolved).loops if run.current_step in loop.jobs
                    )
                    run.current_step = loop.jobs[0]
                    run.context.setdefault("loop_rounds", {})[loop.name] = 0
                    run.context["declarative_epoch"] += 1
                    run.context["approvals"] = []
                    run.context["skip_jobs"] = []
                run.context.pop("technical_failures", None)
                if run.context.get("fail_point"):
                    run.context.setdefault("fail_history", []).append(
                        {
                            **run.context.pop("fail_point"),
                            "action": action,
                            "revision": run.revision,
                            "created_at": self.clock.now(),
                        }
                    )
                run.context["next_poll_at"] = None
                run.status = RunStatus.QUEUED
                run.finished_at, run.last_error = None, None
                # 已自然完成的暂停节点不重放副作用。
                if run.context.pop("completed_while_paused", False) or run.context.pop(
                    "completed_while_cancelled", False
                ):
                    run.status, run.finished_at = RunStatus.SUCCEEDED, self.clock.now()
                event_type = EventType.RUN_RESUMED
            elif action == "cancel":
                run.status, run.finished_at = RunStatus.CANCELLED, self.clock.now()
                run.context["cancel_requested_at"] = self.clock.now()
                event_type = EventType.RUN_CANCELLED
            else:
                run.context["paused_from"] = run.status.value
                run.context["pause_requested_at"] = self.clock.now()
                run.status = RunStatus.PAUSED
                _, resolved = self.definitions.read(run_id, run.context["workflow_definition"])
                definition = self.parse(resolved)
                job = next((job for job in definition.jobs if job.name == run.current_step), None)
                if job is not None:
                    loop = next((loop for loop in definition.loops if job.name in loop.jobs), None)
                    run.context["pause_point"] = {
                        "stage": job.stage,
                        "job": job.name,
                        "round": run.context.get("loop_rounds", {}).get(loop.name, 0) + 1
                        if loop
                        else 1,
                        "reason": "user",
                        "created_at": self.clock.now(),
                        "revision": run.revision,
                        "session_id": None,
                    }
                event_type = EventType.RUN_PAUSED
            run.updated_at = self.clock.now()
            self.store.update_run(run)
            event = self.events.make(event_type, run_id, payload={"action": action})
            self.store.save_event(event)
        self.events.publish([event])
        return run
