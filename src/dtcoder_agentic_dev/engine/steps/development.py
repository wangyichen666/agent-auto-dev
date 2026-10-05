"""默认产品的 Codex 步骤，Git 和验证命令仅通过专用构造参数提供。"""

import json
from pathlib import Path

from dtcoder_agentic_dev.domain.errors import FatalError, TechnicalError
from dtcoder_agentic_dev.domain.json import strict_json_loads
from dtcoder_agentic_dev.domain.workflow import OutcomeType, StepOutcome
from dtcoder_agentic_dev.ports.agent_executor import AgentExecutionRequest


def changes_directory(request):
    return f".agents/changes/issue-{request.issue.number}"


def _read(store, workspace, relative):
    text = store.read_text(workspace, relative)
    if not text.strip():
        raise TechnicalError(f"产物为空：{relative}")
    return text


def parse_review_summary(text, expected_round):
    try:
        data = strict_json_loads(text)
    except (ValueError, TypeError) as exc:
        raise TechnicalError("代码评审 summary.json 必须是纯 JSON 对象") from exc
    required = {"passed", "round", "blockers", "majors", "minors"}
    if not isinstance(data, dict) or set(data) != required:
        raise TechnicalError("评审 summary.json 字段缺失或存在未知字段")
    if (
        type(data["passed"]) is not bool
        or type(data["round"]) is not int
        or data["round"] != expected_round
    ):
        raise TechnicalError("评审 passed 或 round 类型/值错误")
    for key in ("blockers", "majors", "minors"):
        if not isinstance(data[key], list) or any(
            not isinstance(v, (str, dict)) for v in data[key]
        ):
            raise TechnicalError(f"评审 {key} 必须是问题列表（字符串或对象）")
    return data


class PromptStep:
    template = ""
    name = ""

    def __init__(self, renderer, git):
        self.renderer, self.git = renderer, git

    def variables(self, request):
        change = changes_directory(request)
        return {
            "issue_number": request.issue.number,
            "issue_title": request.issue.title,
            "issue_body": request.issue.body,
            "issue_url": request.issue.url,
            "workspace_path": request.workspace,
            "change_dir": change,
            "base_branch": request.run.base_branch,
            "base_revision": request.run.context.get("base_revision", request.run.base_branch),
            "branch": request.run.branch,
            "review_round": request.run.context.get("review_round", 0) + 1,
            "review_dir": f"{change}/codereview/round-{request.run.context.get('review_round', 0) + 1}",
            "blockers_json": json.dumps(
                request.run.context.get("blockers", []), ensure_ascii=False
            ),
            "prior_review_dir": request.run.context.get("review_dir", ""),
            "validation_commands": json.dumps(
                request.repository.validation_commands, ensure_ascii=False
            ),
        }

    def input_snapshot(self, request):
        prompt = self.renderer.render(self.template, self.variables(request))
        recovered = request.run.context.get("recovery_inputs", {}).get(self.name, {})
        before = recovered.get("untracked_before")
        if before is None:
            before = sorted(self.git.snapshot_untracked(request.workspace))
        return {
            "template_sha256": prompt.sha256,
            "template_path": prompt.template_path,
            "untracked_before": before,
        }

    def invoke(self, request, services):
        prompt = self.renderer.render(self.template, self.variables(request))
        services.agent_executor.execute(
            AgentExecutionRequest(
                request.run.run_id,
                self.name,
                request.attempt_number,
                request.workspace,
                prompt.text,
            )
        )

    def capture(self, request, services, paths):
        return [
            services.artifact_store.capture(
                request.workspace,
                p,
                run_id=request.run.run_id,
                step_name=self.name,
                kind=Path(p).suffix.lstrip("."),
            )
            for p in paths
        ]

    def commit(self, request, paths):
        return self.git.commit_changes(
            request.workspace,
            f"DTCoder {self.name} issue #{request.issue.number}",
            artifact_paths=paths,
            untracked_before=set(request.input_snapshot.get("untracked_before", [])),
        )


class RequirementsStep(PromptStep):
    name, template = "requirements", "requirements"

    def execute(self, request, services):
        change = changes_directory(request)
        paths = [f"{change}/{filename}" for filename in ("spec.md", "plan.md", "tasks.md")]
        valid = True
        for p in paths:
            try:
                _read(services.artifact_store, request.workspace, p)
            except TechnicalError:
                valid = False
        if not valid:
            services.artifact_store.ensure_parent(request.workspace, f"{change}/spec.md")
            self.invoke(request, services)
        for path in paths:
            _read(services.artifact_store, request.workspace, path)
        artifacts = self.capture(request, services, paths)
        self.commit(request, paths)
        return StepOutcome(
            OutcomeType.SUCCEEDED, artifacts=artifacts, facts={"requirements_reused": valid}
        )


class CodingStep(PromptStep):
    name, template = "coding", "coding"

    def __init__(self, renderer, git, commands):
        super().__init__(renderer, git)
        self.commands = commands

    def validate(self, request):
        for command in request.repository.validation_commands:
            self.commands.run(
                command, cwd=request.workspace, timeout=request.repository.validation_timeout
            )

    def execute(self, request, services):
        change = changes_directory(request)
        for name in ("spec.md", "plan.md", "tasks.md"):
            _read(services.artifact_store, request.workspace, f"{change}/{name}")
        self.invoke(request, services)
        status = self.git.status_porcelain(request.workspace)
        if not status.strip():
            # 崩溃可能发生在宿主提交后，已有业务 diff 可继续评审。
            names = self.git.diff_name_only(
                request.workspace, request.run.context.get("base_revision", request.run.base_branch)
            )
            if request.run.context.get("recovery_inputs", {}).get(self.name) and any(
                not p.startswith(".agents/") for p in names
            ):
                self.validate(request)
                return StepOutcome(OutcomeType.SKIPPED, facts={"coding_recovered": True})
            raise FatalError("编码步骤没有产生代码变化")
        changed = []
        for line in status.splitlines():
            if len(line) >= 4:
                changed.append(line[3:].strip('"'))
        if not any(not p.startswith(".agents/") for p in changed):
            raise FatalError("编码步骤仅修改了代理产物，没有业务代码变化")
        self.validate(request)
        self.commit(request, [])
        return StepOutcome(OutcomeType.SUCCEEDED, facts={"validation_passed": True})


class ReviewStep(PromptStep):
    name, template = "review", "code_review"

    def execute(self, request, services):
        variables = self.variables(request)
        directory = variables["review_dir"]
        report, summary = f"{directory}/report.md", f"{directory}/summary.json"
        services.artifact_store.ensure_parent(request.workspace, report)
        # 仅有效的本轮产物允许复用，技术重试不会增加 round。
        try:
            _read(services.artifact_store, request.workspace, report)
            data = parse_review_summary(
                _read(services.artifact_store, request.workspace, summary),
                variables["review_round"],
            )
        except TechnicalError:
            self.invoke(request, services)
            _read(services.artifact_store, request.workspace, report)
            data = parse_review_summary(
                _read(services.artifact_store, request.workspace, summary),
                variables["review_round"],
            )
        artifacts = self.capture(request, services, [report, summary])
        self.commit(request, [report, summary])
        blocked = bool(data["blockers"]) or not data["passed"]
        return StepOutcome(
            OutcomeType.BLOCKED if blocked else OutcomeType.SUCCEEDED,
            artifacts=artifacts,
            facts={
                "review_round": data["round"],
                "blockers": data["blockers"],
                "review_dir": directory,
                "review_summary": data,
            },
        )


class FixStep(CodingStep):
    name, template = "fix", "fix_loop"

    def __init__(self, renderer, git, commands, max_fix_loops):
        super().__init__(renderer, git, commands)
        self.max_fix_loops = max_fix_loops

    def execute(self, request, services):
        loops = request.run.context.get("fix_loops", 0)
        if loops >= self.max_fix_loops:
            raise FatalError(f"Blocker 修复循环已达到上限 {self.max_fix_loops}")
        if not request.run.context.get("blockers"):
            raise FatalError("评审未通过但没有可修复的 Blocker，需要人工处理")
        self.invoke(request, services)
        self.validate(request)
        if not self.git.status_porcelain(request.workspace).strip() and not request.run.context.get(
            "recovery_inputs", {}
        ).get(self.name):
            raise FatalError("Blocker 修复未产生任何变更")
        self.commit(request, [])
        return StepOutcome(
            OutcomeType.SUCCEEDED, facts={"fix_loops": loops + 1, "validation_passed": True}
        )
