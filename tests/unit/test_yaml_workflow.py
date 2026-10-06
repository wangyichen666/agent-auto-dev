"""声明式工作流的行为契约：严格输入、安全引用、注册及恢复。"""

import json

import pytest
import yaml

from dtcoder_agentic_dev.adapters.tools.local import LocalTools
from dtcoder_agentic_dev.application.yaml_workflows import compile_workflow, parse_workflow
from dtcoder_agentic_dev.domain.errors import ConfigurationError, TechnicalError
from dtcoder_agentic_dev.domain.workflow import OutcomeType, StepRequest, StepServices
from dtcoder_agentic_dev.engine.registry import ExecutorRegistry
from dtcoder_agentic_dev.infrastructure.filesystem.artifacts import FileArtifactStore


def document():
    return {
        "name": "文件任务",
        "version": "1",
        "stages": ["prepare", "verify"],
        "jobs": {
            "write": {
                "stage": "prepare",
                "type": "tool",
                "toolName": "file.write",
                "config": {"path": "result.json", "content": '{"passed": true}'},
                "outputs": {"result": {"path": "result.json", "parameters": ["passed"]}},
            },
            "verify": {
                "stage": "verify",
                "agent": "default",
                "prompt": "检查产物",
                "inputs": {"previous": {"job": "write", "output": "result"}},
            },
        },
    }


def parse(data):
    return parse_workflow(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        agents={"default"},
        tools={"file.write", "command"},
    )


def test_stage_order_overrides_interleaved_jobs():
    data = document()
    data["jobs"] = dict(reversed(list(data["jobs"].items())))
    assert [j.name for j in parse(data).ordered_jobs] == ["write", "verify"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(typo=True),
        lambda d: d["jobs"]["verify"].update(agent="missing"),
        lambda d: d["jobs"]["verify"].update(stage="missing"),
        lambda d: d["jobs"]["verify"].update(skill="检查.md"),
        lambda d: d["jobs"]["verify"].pop("prompt"),
        lambda d: d["jobs"]["write"].update(toolName="unknown"),
        lambda d: d["jobs"]["write"]["outputs"]["result"].update(path="../outside"),
        lambda d: d["jobs"]["write"]["outputs"]["result"].update(path="/absolute"),
        lambda d: d["jobs"]["verify"]["inputs"]["previous"].update(output="missing"),
        lambda d: d["jobs"]["verify"].update(config={"execute": {"timeout": True}}),
        lambda d: d["jobs"]["verify"].update(config={"execute": {"retries": -1}}),
        lambda d: d["jobs"]["verify"].update(config={"execute": {"humanAgentType": "maybe"}}),
    ],
)
def test_invalid_definition_rejected(mutate):
    data = document()
    mutate(data)
    with pytest.raises(ConfigurationError):
        parse(data)


def test_duplicate_yaml_keys_and_recursive_alias_rejected():
    with pytest.raises(ConfigurationError, match="重复"):
        parse_workflow("name: a\nname: b", agents=set(), tools=set())
    with pytest.raises(ConfigurationError):
        parse_workflow("context: &a {self: *a}", agents=set(), tools=set())


def test_registry_rejects_unknown_and_duplicate():
    registry = ExecutorRegistry()
    registry.register_tool("command", lambda *a: None)
    with pytest.raises(ConfigurationError):
        registry.register_tool("command", lambda *a: None)
    with pytest.raises(ConfigurationError):
        registry.resolve_agent("missing")


def test_loop_condition_requires_declared_parameters():
    data = document()
    data["loops"] = [
        {
            "name": "check",
            "jobs": ["write"],
            "max_rounds": 2,
            "until": {"job": "write", "parameter": "unknown", "equals": True},
        }
    ]
    with pytest.raises(ConfigurationError, match="参数"):
        parse(data)
    data["loops"][0]["until"]["parameter"] = "passed"
    assert parse(data).loops[0].max_rounds == 2
    data["loops"][0]["until"] = "__import__('os').system('true')"
    with pytest.raises(ConfigurationError):
        parse(data)


def test_file_tool_outputs_and_symlink_boundary(tmp_path, run, issue, clock, ids):
    registry = ExecutorRegistry()
    LocalTools(None).register(registry)
    registry.register_agent("default", object())
    workflow = parse(document())
    graph, steps = compile_workflow(workflow, registry)
    run.workspace_path = str(tmp_path)
    request = StepRequest(run, issue, None, str(tmp_path), {}, 1)
    services = StepServices(None, FileArtifactStore(clock, ids), clock)
    result = steps.resolve(graph.nodes["write"].step).execute(request, services)
    assert result.type is OutcomeType.SUCCEEDED
    assert result.facts["parameters"]["write"]["passed"] is True
    assert json.loads((tmp_path / "result.json").read_text())["passed"]
    outside = tmp_path.parent / "outside-result.json"
    outside.write_text("安全边界")
    (tmp_path / "result.json").unlink()
    (tmp_path / "result.json").symlink_to(outside)
    with pytest.raises(TechnicalError):
        steps.resolve("write").execute(request, services)
    assert outside.read_text() == "安全边界"


@pytest.mark.parametrize(
    "rule,content,passed",
    [
        ({"type": "exists", "path": "result.txt"}, "", True),
        ({"type": "nonempty", "path": "result.txt"}, "  ", False),
        ({"type": "sections", "path": "result.txt", "sections": ["结论"]}, "# 结论\n事实", True),
        ({"type": "regex", "path": "result.txt", "pattern": "结果：[0-9]+"}, "结果：42", True),
        (
            {"type": "json", "path": "result.txt", "values": {"passed": True}},
            '{"passed": 1}',
            False,
        ),
        ({"type": "json", "path": "result.txt", "fields": ["missing"]}, "{}", False),
    ],
)
def test_artifact_rules(rule, content, passed, tmp_path, run, issue, clock, ids):
    from dtcoder_agentic_dev.domain.errors import BusinessError

    data = document()
    data["jobs"].pop("verify")
    job = data["jobs"]["write"]
    job["config"].update(path="result.txt", content=content)
    job["outputs"] = {"result": "result.txt"}
    job["artifact_check"] = [rule]
    registry = ExecutorRegistry()
    LocalTools(None).register(registry)
    _, steps = compile_workflow(parse(data), registry)
    request = StepRequest(run, issue, None, str(tmp_path), {}, 1)
    services = StepServices(None, FileArtifactStore(clock, ids), clock)
    if passed:
        assert steps.resolve("write").execute(request, services).type is OutcomeType.SUCCEEDED
    else:
        with pytest.raises(BusinessError):
            steps.resolve("write").execute(request, services)


@pytest.mark.parametrize(
    "fragment",
    [
        "agent: []",
        "type: {}",
        "stage: []",
        "model: false",
        "toolName: []",
        "config: {execute: {humanAgentType: []}}",
        "outputs: {result: {path: result.txt, parameters: [passed, passed]}}",
    ],
)
def test_wrong_types_raise_actionable_configuration_error(fragment):
    base = "name: type-test\nversion: '1'\nstages: [write]\njobs:\n  write:\n    stage: write\n    prompt: 测试\n"
    with pytest.raises(ConfigurationError):
        parse_workflow(base + "    " + fragment + "\n", agents={"default"}, tools=set())


def test_post_actions_cannot_invalidate_already_checked_output(tmp_path, run, issue, clock, ids):
    from dtcoder_agentic_dev.domain.errors import BusinessError

    data = document()
    data["jobs"].pop("verify")
    job = data["jobs"]["write"]
    job["artifact_check"] = [{"type": "json", "path": "result.json", "fields": ["passed"]}]
    job["post_actions"] = [
        {"toolName": "file.write", "config": {"path": "result.json", "content": "失效"}}
    ]
    registry = ExecutorRegistry()
    LocalTools(None).register(registry)
    _, steps = compile_workflow(parse(data), registry)
    with pytest.raises(BusinessError):
        steps.resolve("write").execute(
            StepRequest(run, issue, None, str(tmp_path), {}, 1),
            StepServices(None, FileArtifactStore(clock, ids), clock),
        )


@pytest.mark.parametrize("path", [".git/config", ".workflow/owner.json", "a/../../x", "a\\b"])
def test_reserved_and_escape_output_paths_rejected(path):
    data = document()
    data["jobs"]["write"]["outputs"]["result"]["path"] = path
    with pytest.raises(ConfigurationError):
        parse(data)


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_nonfinite_yaml_values_rejected_everywhere(value):
    data = document()
    data["loops"] = [
        {
            "name": "finite",
            "jobs": ["write"],
            "max_rounds": 2,
            "until": {"job": "write", "parameter": "passed", "equals": value},
        }
    ]
    with pytest.raises(ConfigurationError):
        parse(data)


@pytest.mark.parametrize("content", ['{"passed": true, "passed": false}', '{"passed": NaN}'])
def test_parameter_json_is_strict(content, tmp_path, run, issue, clock, ids):
    from dtcoder_agentic_dev.domain.errors import BusinessError

    data = document()
    data["jobs"].pop("verify")
    data["jobs"]["write"]["config"]["content"] = content
    registry = ExecutorRegistry()
    LocalTools(None).register(registry)
    _, steps = compile_workflow(parse(data), registry)
    with pytest.raises(BusinessError):
        steps.resolve("write").execute(
            StepRequest(run, issue, None, str(tmp_path), {}, 1),
            StepServices(None, FileArtifactStore(clock, ids), clock),
        )


@pytest.mark.parametrize("protected", [".git/config", ".workflow/owner.json"])
def test_symlink_to_protected_file_inside_workspace_rejected(
    protected, tmp_path, run, issue, clock, ids
):
    target = tmp_path / protected
    target.parent.mkdir()
    target.write_text("受管身份")
    (tmp_path / "result.json").symlink_to(target)
    data = document()
    data["jobs"].pop("verify")
    registry = ExecutorRegistry()
    LocalTools(None).register(registry)
    _, steps = compile_workflow(parse(data), registry)
    with pytest.raises(TechnicalError):
        steps.resolve("write").execute(
            StepRequest(run, issue, None, str(tmp_path), {}, 1),
            StepServices(None, FileArtifactStore(clock, ids), clock),
        )
    with pytest.raises(TechnicalError):
        FileArtifactStore(clock, ids).capture(
            str(tmp_path), "result.json", run_id=run.run_id, step_name="write", kind="file"
        )
    assert target.read_text() == "受管身份"
