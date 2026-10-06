"""严格 YAML 解析与编译；执行统一复用 WorkflowRunner。"""

import json
import math
import re
from dataclasses import asdict, replace
from pathlib import PurePosixPath

import yaml

from dtcoder_agentic_dev.domain.errors import ConfigurationError
from dtcoder_agentic_dev.domain.workflow import (
    NodeDefinition,
    OutcomeType,
    Transition,
    WorkflowDefinition,
)
from dtcoder_agentic_dev.domain.workflow.declarative import (
    ExecutionPolicy,
    JobSpec,
    LoopSpec,
    OutputSpec,
    YamlWorkflow,
)
from dtcoder_agentic_dev.engine.declarative import CompleteStep, DeclarativeStep
from dtcoder_agentic_dev.engine.registry import StepRegistry

MAX_YAML_BYTES = 1024 * 1024


class StrictLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str) or key in result:
            raise ConfigurationError("YAML 映射键必须是字符串且不能重复")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def mapping(value, allowed, location):
    if not isinstance(value, dict):
        raise ConfigurationError(f"{location} 必须是映射")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ConfigurationError(f"{location} 未知字段：{', '.join(sorted(unknown))}")
    return value


def text(value, location):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigurationError(f"{location} 必须是非空字符串")
    return value


def identifier(value, location):
    text(value, location)
    if not re.fullmatch(r"[\w-]+", value) or value.startswith("__"):
        raise ConfigurationError(f"{location} 必须是字母、数字、下划线或连字符标识符")
    return value


def relative_path(value, location):
    text(value, location)
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or value == ".":
        raise ConfigurationError(f"{location} 必须是工作区内相对路径，禁止 .. 和绝对路径")
    if any(part in {".git", ".workflow"} for part in path.parts):
        raise ConfigurationError(f"{location} 不得修改受管目录")
    return value


def sequence(value, location):
    if not isinstance(value, list):
        raise ConfigurationError(f"{location} 必须是列表")
    return value


def _policy(config):
    policy = mapping(
        config.get("execute", {}),
        {"humanAgentType", "timeout", "retries", "allow_skip"},
        "config.execute",
    )
    human = policy.get("humanAgentType", "auto")
    timeout, retries = policy.get("timeout", 1800), policy.get("retries", 0)
    skip = policy.get("allow_skip", False)
    if not isinstance(human, str) or human not in {"auto", "notify", "approval", "human"}:
        raise ConfigurationError("humanAgentType 必须是 auto/notify/approval/human")
    if (
        type(timeout) not in {int, float}
        or not math.isfinite(timeout)
        or timeout <= 0
        or type(retries) is not int
        or not 0 <= retries <= 100
        or type(skip) is not bool
    ):
        raise ConfigurationError(
            "timeout 必须为正有限数，retries 为 0..100 整数，allow_skip 为布尔"
        )
    return ExecutionPolicy(human, float(timeout), retries, skip)


def _rules(rules):
    parsed = []
    for rule in sequence(rules, "artifact_check"):
        kind = rule.get("type") if isinstance(rule, dict) else None
        fields = {
            "exists": set(),
            "nonempty": set(),
            "sections": {"sections"},
            "json": {"fields", "values"},
            "regex": {"pattern"},
            "script": {"command"},
        }
        if not isinstance(kind, str) or kind not in fields:
            raise ConfigurationError(
                "artifact_check.type 必须是 exists/nonempty/sections/json/regex/script"
            )
        mapping(rule, {"type", "path"} | fields[kind], "artifact_check")
        relative_path(rule.get("path"), "artifact_check.path")
        if kind in {"sections", "json"}:
            key = "sections" if kind == "sections" else "fields"
            for item in sequence(rule.get(key, []), key):
                text(item, key)
            if kind == "json" and not isinstance(rule.get("values", {}), dict):
                raise ConfigurationError("json.values 必须是映射")
        if kind == "regex":
            try:
                re.compile(text(rule.get("pattern"), "pattern"))
            except re.error as exc:
                raise ConfigurationError("产物正则表达式无效") from exc
        if kind == "script":
            command(rule.get("command"))
        parsed.append(rule)
    return tuple(parsed)


def command(value):
    items = sequence(value, "command")
    if not items or any(not isinstance(v, str) or not v or "\x00" in v for v in items):
        raise ConfigurationError("command 必须是非空字符串参数数组")
    return items


def _tool_config(name, config):
    if not isinstance(config, dict):
        raise ConfigurationError(f"工具 {name}.config 必须是映射")
    fields = {
        "file.write": {"path", "content"},
        "file.copy": {"source", "target"},
        "command": {"command"},
        "test": {"command"},
    }
    if name not in fields:
        return  # 第三方工具由注册的 Port 实现验证自己的参数。
    mapping(config, fields[name] | {"execute"}, f"工具 {name}.config")
    for key in fields[name]:
        if key not in config:
            raise ConfigurationError(f"工具 {name}.config 缺少 {key}")
        if key == "command":
            command(config[key])
        elif key == "content":
            if not isinstance(config[key], str):
                raise ConfigurationError("file.write.content 必须是字符串")
        else:
            relative_path(config[key], key)


def parse_workflow(source: str, *, agents: set[str], tools: set[str]) -> YamlWorkflow:
    if len(source.encode("utf-8")) > MAX_YAML_BYTES:
        raise ConfigurationError("工作流 YAML 超过 1 MiB 限制")
    # 拒绝别名，避免递归/展开炸弹与复杂合并覆盖语义。
    try:
        if any(
            isinstance(token, (yaml.AliasToken, yaml.AnchorToken)) for token in yaml.scan(source)
        ):
            raise ConfigurationError("工作流不支持 YAML anchor/alias，请显式填写节点")
        data = yaml.load(source, Loader=StrictLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise ConfigurationError("YAML 格式错误，请检查缩进、键和字段类型") from exc
    data = mapping(
        data,
        {"name", "version", "description", "context", "stages", "jobs", "loops", "metadata"},
        "workflow",
    )
    try:
        json.dumps(data, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ConfigurationError(
            "工作流只支持有限 JSON 值，禁止日期对象、非有限数或过深嵌套"
        ) from exc
    name, version = text(data.get("name"), "name"), text(data.get("version"), "version")
    stages = tuple(identifier(s, "stage") for s in sequence(data.get("stages"), "stages"))
    if not stages or len(set(stages)) != len(stages) or len(stages) > 256:
        raise ConfigurationError("stages 不能为空或重复")
    jobs_data = data.get("jobs")
    if not isinstance(jobs_data, dict) or not jobs_data or len(jobs_data) > 2048:
        raise ConfigurationError("jobs 必须是非空映射")
    jobs = []
    for key, value in jobs_data.items():
        identifier(key, "job")
        value = mapping(
            value,
            {
                "stage",
                "type",
                "agent",
                "toolName",
                "skill",
                "prompt",
                "model",
                "inputs",
                "outputs",
                "artifact_check",
                "post_actions",
                "config",
            },
            f"jobs.{key}",
        )
        if value.get("stage") not in stages:
            raise ConfigurationError(f"job {key} 引用未知 stage；可用：{', '.join(stages)}")
        kind = value.get("type", "agent")
        text(kind, "job.type")
        agent, tool = value.get("agent", "default"), value.get("toolName")
        config = value.get("config", {})
        if not isinstance(config, dict):
            raise ConfigurationError("job.config 必须是映射")
        policy = _policy(config)
        if kind == "agent":
            text(agent, "agent")
            if agent not in agents:
                raise ConfigurationError(f"job {key} 引用未知 agent：{agent}")
            if ("skill" in value) == ("prompt" in value) or "toolName" in value:
                raise ConfigurationError(f"job {key} 的 skill 与 prompt 必须且只能配置一项")
            mapping(config, {"execute"}, "agent.config")
            if "prompt" in value:
                text(value["prompt"], "prompt")
            else:
                relative_path(value["skill"], "skill")
            if "model" in value:
                text(value["model"], "model")
        elif kind == "tool":
            text(tool, "toolName")
            if tool not in tools:
                raise ConfigurationError(f"job {key} 引用未知 tool：{tool}")
            if any(k in value for k in ("agent", "prompt", "skill", "model")):
                raise ConfigurationError("tool job 不能包含 agent/prompt/skill/model")
            _tool_config(tool, config)
        else:
            raise ConfigurationError("job.type 必须是 agent 或 tool")
        outputs = {}
        declared_parameters = set()
        if not isinstance(value.get("outputs", {}), dict):
            raise ConfigurationError("outputs 必须是映射")
        for alias, output in value.get("outputs", {}).items():
            identifier(alias, "output")
            output = {"path": output} if isinstance(output, str) else output
            output = mapping(output, {"path", "kind", "parameters"}, "output")
            parameters = tuple(
                text(p, "parameter") for p in sequence(output.get("parameters", []), "parameters")
            )
            if len(set(parameters)) != len(parameters) or declared_parameters.intersection(
                parameters
            ):
                raise ConfigurationError("同一 job 的参数名称不能重复")
            declared_parameters.update(parameters)
            outputs[alias] = OutputSpec(
                relative_path(output.get("path"), "output.path"),
                text(output.get("kind", "file"), "output.kind"),
                parameters,
            )
        inputs = value.get("inputs", {})
        if not isinstance(inputs, dict):
            raise ConfigurationError("inputs 必须是映射")
        for alias, reference in inputs.items():
            identifier(alias, "input")
            mapping(reference, {"job", "output"}, "input")
            text(reference.get("job"), "input.job")
            text(reference.get("output"), "input.output")
        actions = []
        for action in sequence(value.get("post_actions", []), "post_actions"):
            mapping(action, {"toolName", "config"}, "post_action")
            text(action.get("toolName"), "post_action.toolName")
            if action.get("toolName") not in tools:
                raise ConfigurationError("post_action 引用未知 tool")
            _tool_config(action["toolName"], action.get("config", {}))
            if "execute" in action.get("config", {}):
                raise ConfigurationError("post_action 共用 job 执行策略，不能单独设置 execute")
            actions.append(action)
        jobs.append(
            JobSpec(
                key,
                value["stage"],
                kind,
                agent,
                tool,
                value.get("prompt"),
                value.get("skill"),
                value.get("model"),
                inputs,
                outputs,
                _rules(value.get("artifact_check", [])),
                tuple(actions),
                config,
                policy,
            )
        )
        if (
            any(rule["type"] == "script" for rule in jobs[-1].artifact_check)
            and "command" not in tools
        ):
            raise ConfigurationError("script 产物规则需要注册 command 工具")
    context, metadata = data.get("context", {}), data.get("metadata", {})
    if not isinstance(context, dict) or not isinstance(metadata, dict):
        raise ConfigurationError("context 和 metadata 必须是映射")
    try:
        json.dumps([context, metadata], allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("context 和 metadata 必须只包含有限 JSON 数据") from exc
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ConfigurationError("description 必须是字符串")
    workflow = YamlWorkflow(name, version, description, context, stages, tuple(jobs), (), metadata)
    ordered = workflow.ordered_jobs
    positions = {j.name: i for i, j in enumerate(ordered)}
    by_name = {j.name: j for j in jobs}
    for job in jobs:
        for ref in job.inputs.values():
            producer = by_name.get(ref["job"])
            if not producer or ref["output"] not in producer.outputs:
                raise ConfigurationError(f"job {job.name} 的 input 引用未声明的 job/output")
            if positions[producer.name] >= positions[job.name]:
                raise ConfigurationError("input producer 必须位于 consumer 之前")
    loops, occupied = [], set()
    for loop in sequence(data.get("loops", []), "loops"):
        mapping(loop, {"name", "jobs", "max_rounds", "until", "on_exhausted"}, "loop")
        loop_name = identifier(loop.get("name"), "loop.name")
        members = tuple(sequence(loop.get("jobs"), "loop.jobs"))
        for member in members:
            text(member, "loop.jobs")
        if not members or any(m not in positions for m in members):
            raise ConfigurationError("loop.jobs 必须引用已声明 job")
        indexes = [positions[m] for m in members]
        if indexes != list(range(indexes[0], indexes[0] + len(indexes))) or occupied.intersection(
            members
        ):
            raise ConfigurationError("loop.jobs 必须连续且有序，不能重叠或嵌套")
        if any(existing.name == loop_name for existing in loops):
            raise ConfigurationError("loop 名称重复")
        rounds, exhausted = loop.get("max_rounds"), loop.get("on_exhausted", "fail")
        text(exhausted, "loop.on_exhausted")
        if type(rounds) is not int or not 1 <= rounds <= 1000 or exhausted not in {"pause", "fail"}:
            raise ConfigurationError("max_rounds 必须为 1..1000 整数，on_exhausted 为 pause/fail")
        until = mapping(loop.get("until"), {"job", "parameter", "equals"}, "loop.until")
        producer, parameter = until.get("job"), until.get("parameter")
        text(producer, "loop.until.job")
        text(parameter, "loop.until.parameter")
        if producer not in members or parameter not in {
            p for o in by_name[producer].outputs.values() for p in o.parameters
        }:
            raise ConfigurationError("loop.until 必须引用循环内已声明的参数产物")
        if "equals" not in until or type(until["equals"]) not in {
            str,
            int,
            float,
            bool,
            type(None),
        }:
            raise ConfigurationError("loop.until.equals 必须为 JSON 标量")
        loops.append(
            LoopSpec(loop_name, members, rounds, producer, parameter, until["equals"], exhausted)
        )
        occupied.update(members)
    return YamlWorkflow(
        name, version, description, context, stages, tuple(jobs), tuple(loops), metadata
    )


def resolved_yaml(workflow):
    jobs = {}
    for job in workflow.jobs:
        entry = {
            "stage": job.stage,
            "type": job.type,
            "inputs": job.inputs,
            "outputs": {
                k: {**asdict(o), "parameters": list(o.parameters)} for k, o in job.outputs.items()
            },
            "artifact_check": list(job.artifact_check),
            "post_actions": list(job.post_actions),
            "config": {
                **job.config,
                "execute": {
                    "humanAgentType": job.policy.human_agent_type,
                    "timeout": job.policy.timeout,
                    "retries": job.policy.retries,
                    "allow_skip": job.policy.allow_skip,
                },
            },
        }
        if job.type == "agent":
            entry.update(agent=job.agent)
            entry["prompt" if job.prompt is not None else "skill"] = job.prompt or job.skill
            if job.model:
                entry["model"] = job.model
        else:
            entry["toolName"] = job.tool_name
        jobs[job.name] = entry
    data = {
        "name": workflow.name,
        "version": workflow.version,
        "description": workflow.description,
        "context": workflow.context,
        "stages": list(workflow.stages),
        "jobs": jobs,
        "loops": [
            {
                "name": loop_entry.name,
                "jobs": list(loop_entry.jobs),
                "max_rounds": loop_entry.max_rounds,
                "until": {
                    "job": loop_entry.producer,
                    "parameter": loop_entry.parameter,
                    "equals": loop_entry.equals,
                },
                "on_exhausted": loop_entry.on_exhausted,
            }
            for loop_entry in workflow.loops
        ],
        "metadata": workflow.metadata,
    }
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def compile_workflow(workflow, executors, *, skill_reader=None, repo_available=None):
    registry, nodes, transitions = StepRegistry(), {}, []
    ordered = workflow.ordered_jobs
    for index, job in enumerate(ordered):
        tool_configs = [(job.tool_name, job.config)] if job.type == "tool" else []
        tool_configs.extend(
            (action["toolName"], action.get("config", {})) for action in job.post_actions
        )
        tool_configs.extend(
            ("command", {"command": rule["command"]})
            for rule in job.artifact_check
            if rule["type"] == "script"
        )
        for tool_name, tool_config in tool_configs:
            executor = executors.resolve_tool(tool_name)
            requires_repo = getattr(executor, "requires_repository", False)
            if callable(requires_repo):
                requires_repo = requires_repo(tool_config)
            if repo_available is False and requires_repo:
                raise ConfigurationError(f"无仓工作流不能执行依赖仓库的工具：{tool_name}")
        loop = next(
            (loop_entry for loop_entry in workflow.loops if job.name in loop_entry.jobs), None
        )
        step = DeclarativeStep(job, workflow, executors, loop, skill_reader)
        registry.register(step)
        nodes[job.name] = NodeDefinition(job.name, job.name)
        following = ordered[index + 1].name if index + 1 < len(ordered) else "__complete"
        if loop and job.name == loop.jobs[-1]:
            transitions.extend(
                [
                    Transition(
                        job.name,
                        loop.jobs[0],
                        condition=lambda c, key=loop.name: (
                            c.get("loop_decisions", {}).get(key) == "repeat"
                        ),
                    ),
                    Transition(
                        job.name,
                        following,
                        condition=lambda c, key=loop.name: (
                            c.get("loop_decisions", {}).get(key) == "exit"
                        ),
                    ),
                ]
            )
        else:
            transitions.append(Transition(job.name, following))
    transitions.extend(
        replace(transition, on=OutcomeType.SKIPPED) for transition in list(transitions)
    )
    registry.register(CompleteStep())
    nodes["__complete"] = NodeDefinition("__complete", "__complete", True)
    graph = WorkflowDefinition(workflow.name, workflow.version, ordered[0].name, nodes, transitions)
    registry.validate(graph)
    return graph, registry
