"""诊断只运行只读命令，输出不含 CLI 响应。"""

import os
from pathlib import Path
from string import Formatter
from types import SimpleNamespace

from dtcoder_agentic_dev.application.dto import Diagnostic
from dtcoder_agentic_dev.application.services.event_views import DEFAULT_TEMPLATES
from dtcoder_agentic_dev.domain.models import Issue, WorkflowRun
from dtcoder_agentic_dev.engine.steps.development import PromptStep
from dtcoder_agentic_dev.prompts.renderer import StrictPromptRenderer


def diagnose(runtime):
    config = runtime.config
    results = [Diagnostic("配置", True, "YAML 与字段校验通过", "通过")]
    try:
        with runtime.store.transaction():
            runtime.store.connection.execute("SELECT version FROM schema_version").fetchone()
        results.append(Diagnostic("SQLite", True, "schema 和事务正常", "通过"))
    except Exception:
        results.append(Diagnostic("SQLite", False, "数据库检查失败", "配置错误"))
    if not config.dry_run:
        for name, binary, enabled, login in [
            ("Git", config.git.binary, True, None),
            (
                "Claude Code",
                config.claude.binary,
                config.agents.default_engine in {"claude-cli", "claude-sdk"}
                or any(e.startswith("claude-") for e in config.agents.model_routes.values()),
                ["auth", "status"],
            ),
            (
                "Codex",
                config.codex.binary,
                config.agents.default_engine == "codex-cli"
                or "codex-cli" in config.agents.model_routes.values(),
                ["login", "status"],
            ),
            (
                "AntCode",
                config.code_host.binary,
                config.code_host.adapter == "antcode",
                ["auth", "status"],
            ),
            (
                "ACI",
                config.pipeline.binary,
                config.pipeline.adapter == "aci"
                and any(r.pipeline_enabled for r in config.repositories),
                ["auth", "status"],
            ),
        ]:
            if not enabled:
                results.append(Diagnostic(name, True, "未启用", "未启用"))
                continue
            prefix = [binary]
            if name == "AntCode" and config.code_host.profile:
                prefix += ["--profile", config.code_host.profile]
            try:
                runtime.commands.run([*prefix, "--version"], timeout=10)
            except Exception:
                results.append(Diagnostic(name, False, "命令不可用", "缺失"))
                continue
            try:
                if login:
                    response = runtime.commands.run([*prefix, *login], timeout=10, check=False)
                    if response.returncode:
                        results.append(Diagnostic(name, False, "请使用该 CLI 完成登录", "未认证"))
                        continue
                results.append(
                    Diagnostic(name, True, "命令及登录状态通过" if login else "命令可用", "通过")
                )
            except Exception:
                results.append(Diagnostic(name, False, "登录检查失败；请检查 CLI 版本", "配置错误"))
    for name, directory in [
        ("状态目录", config.state.directory),
        ("镜像目录", config.workspace.mirrors),
        ("工作区目录", config.workspace.workspaces),
    ]:
        ok = Path(directory).is_dir() and os.access(directory, os.R_OK | os.W_OK | os.X_OK)
        results.append(
            Diagnostic(name, ok, "可读写" if ok else "缺失或不可写", "通过" if ok else "缺失")
        )
    for name, directory, templates in [
        (
            "模板",
            config.prompts.directory,
            ["requirements", "coding", "code_review", "fix_loop", "pr_create"],
        ),
        ("评论模板", config.notification.templates_directory, DEFAULT_TEMPLATES),
    ]:
        missing = [n for n in templates if not (Path(directory) / f"{n}.txt").is_file()]
        invalid = []
        for template_name in templates:
            if template_name in missing:
                continue
            try:
                template_path = (Path(directory) / f"{template_name}.txt").resolve()
                if template_path.parent != Path(directory).resolve():
                    raise ValueError
                text = template_path.read_text(encoding="utf-8")
                if not text.strip():
                    raise ValueError
                variables = {}
                for _, key, spec, conversion in Formatter().parse(text):
                    if key is not None:
                        if not key.isidentifier() or spec or conversion:
                            raise ValueError
                        variables[key] = "诊断"
                if name == "评论模板":
                    variables = {
                        key: "诊断"
                        for key in (
                            "run_id",
                            "branch",
                            "step",
                            "round",
                            "artifacts",
                            "pipeline_url",
                            "pr_url",
                            "error_type",
                            "result",
                        )
                    }
                else:
                    variables = PromptStep(None, None).variables(
                        SimpleNamespace(
                            issue=Issue("diagnostic", "diagnostic", "1", 1, "模板检查"),
                            run=WorkflowRun(
                                "diagnostic", "issue-development", "1", "diagnostic", "1", 1
                            ),
                            workspace=config.workspace.workspaces,
                            repository=SimpleNamespace(validation_commands=[]),
                        )
                    )
                StrictPromptRenderer(directory).render(template_name, variables)
            except Exception:
                invalid.append(template_name)
        ok = not missing and not invalid
        results.append(
            Diagnostic(
                name,
                ok,
                "完整且格式正确" if ok else "缺少或无效模板：" + ",".join(missing + invalid),
                "通过" if ok else "缺失" if missing else "配置错误",
            )
        )
    configured = config.dry_run or config.code_host.adapter != "logging"
    results.append(
        Diagnostic(
            "代码托管",
            configured,
            "已配置" if configured else "未配置真实 CodeHost 适配器",
            "通过" if configured else "未启用",
        )
    )
    results.append(
        Diagnostic(
            "仓库",
            bool(config.repositories),
            f"已配置 {len(config.repositories)} 个仓库",
            "通过" if config.repositories else "配置错误",
        )
    )
    if not config.dry_run:
        for repo in config.repositories:
            try:
                if repo.path:
                    if not runtime.git.is_repository(repo.path):
                        raise ValueError
                    remote = runtime.commands.run(
                        [config.git.binary, "remote", "get-url", "origin"],
                        cwd=repo.path,
                        timeout=config.git.timeout,
                    ).stdout.strip()
                    if remote != repo.remote:
                        raise ValueError
                result = runtime.commands.run(
                    [
                        config.git.binary,
                        "ls-remote",
                        "--exit-code",
                        "--",
                        repo.remote,
                        f"refs/heads/{repo.base_branch}",
                    ],
                    timeout=config.git.timeout,
                    check=False,
                )
                ok = result.returncode == 0 and bool(result.stdout.strip())
                results.append(
                    Diagnostic(
                        f"仓库 {repo.name}",
                        ok,
                        "远端与基础分支可读" if ok else "远端不可读、未认证或分支缺失",
                        "通过" if ok else "配置错误",
                    )
                )
            except Exception:
                results.append(
                    Diagnostic(
                        f"仓库 {repo.name}",
                        False,
                        "本地 remote、远端认证或基础分支配置错误",
                        "配置错误",
                    )
                )
    return results
