"""声明式任务命令只调用应用用例。"""

from pathlib import Path

import click

from dtcoder_agentic_dev.domain.errors import ConfigurationError
from dtcoder_agentic_dev.domain.models import RunStatus


def read_definition(path):
    if Path(path).stat().st_size > 1024 * 1024:
        raise ConfigurationError("工作流 YAML 超过 1 MiB 限制")
    return Path(path).read_text(encoding="utf-8")


def register(app):
    @app.command("submit")
    @click.option(
        "--workflow",
        "workflow_path",
        type=click.Path(exists=True, dir_okay=False),
        help="工作流 YAML；默认使用 init 安装的 document.yaml。",
    )
    @click.option("--task", required=True, help="自然语言任务需求。")
    @click.option("--repo", help="可选单仓名称或 ID；不填则分配无仓工作区。")
    @click.option("--queue-only", is_flag=True, help="仅入队，交由 run/run-once 执行。")
    @click.pass_context
    def submit(ctx, workflow_path, task, repo, queue_only):
        """创建自然语言 YAML 任务，并立即执行一个调度周期。"""
        from dtcoder_agentic_dev.cli.app import runtime_for

        runtime = runtime_for(ctx)
        path = workflow_path or str(Path(runtime.config.declarative.directory) / "document.yaml")
        repo_config = runtime.runs.resolve_repository(repo) if repo else None
        run = runtime.declarative.submit(read_definition(path), task, repo_config)
        if not queue_only and not runtime.config.dry_run:
            run = runtime.dispatcher.dispatch_once(run.run_id) or run
        click.echo(f"运行 {run.run_id}：{run.status.value}")
        if run.status is RunStatus.FAILED:
            raise click.ClickException(run.last_error or "运行失败")

    @app.group("workflow")
    def workflow():
        """校验和查看声明式工作流模板。"""

    @workflow.command("validate")
    @click.argument("path", type=click.Path(exists=True, dir_okay=False))
    @click.pass_context
    def validate(ctx, path):
        """严格校验 YAML，校验不执行 agent 或工具。"""
        from dtcoder_agentic_dev.cli.app import runtime_for

        parsed = runtime_for(ctx).declarative.parse(read_definition(path))
        click.echo(
            f"校验通过：{parsed.name} v{parsed.version}，{len(parsed.stages)} 阶段，{len(parsed.jobs)} 节点。"
        )

    @workflow.command("list")
    @click.pass_context
    def list_templates(ctx):
        """列出用户工作流目录中的 YAML 模板。"""
        from dtcoder_agentic_dev.cli.app import runtime_for

        root = Path(runtime_for(ctx).config.declarative.directory)
        for path in sorted(root.glob("*.yaml")):
            click.echo(path.name)
