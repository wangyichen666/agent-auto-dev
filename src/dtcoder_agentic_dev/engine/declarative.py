"""声明式节点适配到统一步骤生命周期；所有 IO 通过 Port 注入。"""

import json
import re
from dataclasses import asdict
from hashlib import sha256

from dtcoder_agentic_dev.domain.errors import (
    AgentCancelled,
    BusinessError,
    ConfigurationError,
    ExternalCommandTimeout,
    TechnicalError,
)
from dtcoder_agentic_dev.domain.json import strict_json_loads
from dtcoder_agentic_dev.domain.security import redact
from dtcoder_agentic_dev.domain.workflow import OutcomeError, OutcomeType, StepOutcome
from dtcoder_agentic_dev.engine.retry import RetryPolicy
from dtcoder_agentic_dev.ports.agent_executor import AgentExecutionRequest, AgentExecutionStatus
from dtcoder_agentic_dev.ports.tool_executor import ToolRequest


class CompleteStep:
    name = "__complete"

    def execute(self, request, services):
        return StepOutcome(OutcomeType.SUCCEEDED)


class DeclarativeStep:
    stable_identity = True

    def __init__(self, job, workflow, executors, loop, skill_reader=None):
        self.name, self.job, self.workflow = job.name, job, workflow
        self.executors, self.loop, self.skill_reader = executors, loop, skill_reader
        self.stage = job.stage
        self.retry_policy = RetryPolicy(job.policy.retries, 0)
        if job.type == "agent":
            self.executors.resolve_agent(job.agent)
        else:
            self.executors.resolve_tool(job.tool_name)

    def round(self, request):
        return (
            request.run.context.get("loop_rounds", {}).get(self.loop.name, 0) + 1
            if self.loop
            else 1
        )

    def recovery_session(self, attempt):
        if self.job.type != "agent" or self.job.policy.human_agent_type == "human":
            return None
        executor = self.executors.resolve_agent(self.job.agent)
        engine = getattr(executor, "engine", self.job.agent)
        if (
            engine == "claude-sdk"
            or not getattr(executor, "supports_resume", False)
            or not attempt.metadata.get("session_id")
            or attempt.metadata.get("engine") != engine
            or attempt.input_snapshot.get("job") != redact(asdict(self.job))
        ):
            raise ConfigurationError("中断 agent 缺少匹配会话或冻结定义，需人工 revise")
        return {
            "job": self.name,
            "session_id": attempt.metadata["session_id"],
            "engine": engine,
            "mode": "recovery",
        }

    def input_snapshot(self, request):
        resolved_inputs = {}
        for alias, ref in self.job.inputs.items():
            producer = next(j for j in self.workflow.jobs if j.name == ref["job"])
            prior = request.prior_outputs.get(producer.name, {})
            if prior.get("type") != "SUCCEEDED":
                raise BusinessError(f"输入 {alias} 的 producer 未成功完成")
            resolved_inputs[alias] = {**ref, "path": producer.outputs[ref["output"]].path}
        prompt = self.job.prompt
        if self.job.skill:
            if self.skill_reader is None:
                raise TechnicalError("skill 未装配受控读取器")
            prompt = self.skill_reader(self.job.skill)
        return {
            "job": asdict(self.job),
            "resolved_inputs": resolved_inputs,
            "prompt_sha256": sha256((prompt or "").encode()).hexdigest(),
            "resolved_prompt": prompt,
            "stage": self.stage,
            "round": self.round(request),
            "engine": getattr(
                self.executors.resolve_agent(self.job.agent), "engine", self.job.agent
            )
            if self.job.type == "agent"
            else self.job.tool_name,
            "model": self.job.model,
        }

    def execute(self, request, services):
        snapshot = request.input_snapshot or self.input_snapshot(request)
        started = services.clock.now()

        def remaining():
            value = self.job.policy.timeout - (services.clock.now() - started)
            if value <= 0:
                raise ExternalCommandTimeout("节点超过执行超时")
            return value

        round_number = self.round(request)
        key = f"{self.name}:{round_number}"
        if key in request.run.context.get("skip_jobs", []):
            return self._finish(
                request,
                [],
                {},
                {"engine": "skipped", "session_id": None},
                round_number,
                OutcomeType.SKIPPED,
            )
        if self.job.policy.human_agent_type in {
            "approval",
            "human",
        } and key not in request.run.context.get("approvals", []):
            return StepOutcome(
                OutcomeType.PAUSED,
                facts={
                    "pause_point": {
                        "stage": self.stage,
                        "job": self.name,
                        "round": round_number,
                        "reason": self.job.policy.human_agent_type,
                        "session_id": None,
                    }
                },
            )
        resume = request.run.context.get("agent_resume", {})
        if resume.get("job") == self.name and resume.get("engine") != snapshot["engine"]:
            raise ConfigurationError("续聊引擎与当前节点不匹配")
        execution = {
            "engine": snapshot["engine"],
            "model": self.job.model,
            "session_id": None,
            "duration_seconds": 0,
        }
        if self.job.policy.human_agent_type == "human":
            execution["engine"] = "human"
        elif self.job.type == "agent":
            prompt = self._prompt(request, snapshot, services.prompt_builder)
            result = self.executors.resolve_agent(self.job.agent).execute(
                AgentExecutionRequest(
                    request.run.run_id,
                    self.name,
                    request.attempt_number,
                    request.workspace,
                    prompt,
                    model=self.job.model,
                    timeout=remaining(),
                    allow_no_repo=request.repository is None,
                    session_id=resume.get("session_id") if resume.get("job") == self.name else None,
                )
            )
            status = getattr(result, "status", AgentExecutionStatus.SUCCEEDED)
            if result.returncode != 0 or status is not AgentExecutionStatus.SUCCEEDED:
                error_class = (
                    AgentCancelled
                    if status is AgentExecutionStatus.CANCELLED
                    else ExternalCommandTimeout
                    if status is AgentExecutionStatus.TIMED_OUT
                    else TechnicalError
                )
                error = error_class(f"agent 执行未成功：{status.value}，退出码 {result.returncode}")
                error.execution_metadata = {
                    "engine": result.structured.get("engine", snapshot["engine"]),
                    "session_id": result.structured.get("session_id"),
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "duration_seconds": result.duration_seconds,
                }
                raise error
            execution.update(
                engine=result.structured.get("engine", snapshot["engine"]),
                session_id=result.structured.get("session_id"),
                duration_seconds=result.duration_seconds,
                stdout=redact(result.stdout),
                stderr=redact(result.stderr),
            )
        else:
            result = self.executors.resolve_tool(self.job.tool_name).execute(
                ToolRequest(request.workspace, self.job.config, remaining())
            )
            execution.update(
                duration_seconds=result.duration_seconds,
                stdout=redact(result.stdout),
                stderr=redact(result.stderr),
            )
        remaining()
        try:
            return self._validate_and_finish(request, services, execution, round_number, remaining)
        except Exception as exc:
            exc.execution_metadata = execution
            raise

    def _validate_and_finish(self, request, services, execution, round_number, remaining):
        # 产物先校验，再执行显式 post_actions；任何失败均不能报告成功。
        for rule in self.job.artifact_check:
            self._check(rule, request, services, remaining())
        for action in self.job.post_actions:
            self.executors.resolve_tool(action["toolName"]).execute(
                ToolRequest(request.workspace, action.get("config", {}), remaining())
            )
        remaining()
        if self.job.post_actions:
            for rule in self.job.artifact_check:
                self._check(rule, request, services, remaining())
        artifacts, parameters = [], {}
        for alias, output in self.job.outputs.items():
            artifacts.append(
                services.artifact_store.capture(
                    request.workspace,
                    output.path,
                    run_id=request.run.run_id,
                    step_name=self.name,
                    kind=output.kind,
                    allow_empty=True,
                )
            )
            artifacts[-1].metadata["output"] = alias
            if output.parameters:
                try:
                    data = strict_json_loads(
                        services.artifact_store.read_text(request.workspace, output.path)
                    )
                except ValueError as exc:
                    raise BusinessError(f"参数产物 {alias} 必须是 JSON 对象") from exc
                if not isinstance(data, dict) or any(p not in data for p in output.parameters):
                    raise BusinessError(f"参数产物 {alias} 缺少声明字段")
                for p in output.parameters:
                    if p in parameters:
                        raise BusinessError(f"重复参数字段：{p}")
                    parameters[p] = data[p]
        return self._finish(request, artifacts, parameters, execution, round_number)

    def _finish(
        self,
        request,
        artifacts,
        parameters,
        execution,
        round_number,
        outcome_type=OutcomeType.SUCCEEDED,
    ):
        all_parameters = {**request.run.context.get("parameters", {}), self.name: parameters}
        facts = {"parameters": all_parameters, "execution": execution}
        if request.run.context.get("agent_resume", {}).get("job") == self.name:
            facts["agent_resume"] = {}
        if self.loop and self.name == self.loop.jobs[-1]:
            rounds = {**request.run.context.get("loop_rounds", {}), self.loop.name: round_number}
            decisions = {**request.run.context.get("loop_decisions", {})}
            facts["loop_rounds"] = rounds
            if self.loop.satisfied(all_parameters):
                decisions[self.loop.name] = "exit"
            elif round_number < self.loop.max_rounds:
                decisions[self.loop.name] = "repeat"
            else:
                if self.loop.on_exhausted == "pause":
                    facts["pause_point"] = {
                        "stage": self.stage,
                        "job": self.name,
                        "round": round_number,
                        "reason": "loop_exhausted",
                        "loop": self.loop.name,
                        "session_id": execution["session_id"],
                    }
                    return StepOutcome(
                        OutcomeType.PAUSED,
                        artifacts,
                        facts,
                        error=OutcomeError("LOOP_EXHAUSTED", "循环轮数耗尽，等待人工修改后重跑"),
                    )
                return StepOutcome(
                    OutcomeType.FAILED,
                    artifacts,
                    facts,
                    error=OutcomeError("LOOP_EXHAUSTED", "循环轮数耗尽且退出条件不满足"),
                )
            facts["loop_decisions"] = decisions
        return StepOutcome(outcome_type, artifacts, facts)

    def _prompt(self, request, snapshot, builder=None):
        context = {
            "task": {"title": request.issue.title, "body": request.issue.body},
            "workspace": request.workspace,
            "repository": request.run.repository_id or None,
            "context": self.workflow.context,
            "inputs": snapshot["resolved_inputs"],
            "upstream": {
                ref["job"]: request.prior_outputs.get(ref["job"], {})
                .get("facts", {})
                .get("parameters", {})
                .get(ref["job"], {})
                for ref in self.job.inputs.values()
            },
            "feedback": [
                entry
                for entry in request.run.context.get("feedback", [])
                if entry.get("job") == self.name
            ],
            "outputs": {k: asdict(o) for k, o in self.job.outputs.items()},
        }
        if builder is not None and self.workflow.metadata.get("prompt_template"):
            return builder.build(
                snapshot["resolved_prompt"], context, self.workflow.metadata["prompt_template"]
            )
        return (
            f"{snapshot['resolved_prompt']}\n\n任务上下文：\n"
            + json.dumps(context, ensure_ascii=False, indent=2)
            + "\n只允许修改当前工作区内声明产物及任务要求的文件；禁止修改 .git/.workflow、用户源仓库、外部路径或执行提交/推送。"
        )

    def _check(self, rule, request, services, timeout):
        path, kind = rule["path"], rule["type"]
        if kind == "exists":
            if not services.artifact_store.exists(request.workspace, path):
                raise BusinessError(f"产物不存在：{path}")
            return
        content = services.artifact_store.read_text(request.workspace, path)
        ok = True
        if kind == "nonempty":
            ok = bool(content.strip())
        elif kind == "sections":
            ok = all(
                re.search(r"(?m)^#{1,6}\s+" + re.escape(s) + r"\s*$", content)
                for s in rule.get("sections", [])
            )
        elif kind == "regex":
            ok = bool(re.search(rule["pattern"], content))
        elif kind == "json":
            try:
                data = strict_json_loads(content)
            except ValueError as exc:
                raise BusinessError(f"产物不是合法 JSON：{path}") from exc
            ok = (
                isinstance(data, dict)
                and all(k in data for k in rule.get("fields", []))
                and all(
                    k in data and type(data[k]) is type(v) and data[k] == v
                    for k, v in rule.get("values", {}).items()
                )
            )
        elif kind == "script":
            self.executors.resolve_tool("command").execute(
                ToolRequest(request.workspace, {"command": rule["command"]}, timeout)
            )
        if not ok:
            raise BusinessError(f"产物校验失败：{kind} / {path}")
