import json
from dataclasses import asdict

import click
from tabulate import tabulate

from dtcoder_agentic_dev.domain.models import ACTIVE_STATUSES


def register(app):
    @app.command("list")
    @click.option("--all", "show_all", is_flag=True, help="包含已终止运行。")
    @click.option("--repo", help="仓库名称或 ID。")
    @click.pass_context
    def list_command(ctx, show_all, repo):
        """列出运行；默认仅展示活动运行。"""
        from dtcoder_agentic_dev.cli.app import runtime_for

        runtime = runtime_for(ctx)
        repo_id = runtime.runs.resolve_repository(repo).repository_id if repo else None
        runs = [
            r for r in runtime.store.list_runs(repo_id) if show_all or r.status in ACTIVE_STATUSES
        ]
        names = {r.repository_id: r.name for r in runtime.config.repositories}
        click.echo(
            tabulate(
                [
                    [
                        r.run_id,
                        names.get(r.repository_id, r.repository_id),
                        r.issue_number,
                        r.status.value,
                        r.current_step,
                        r.updated_at,
                        r.last_error or "",
                    ]
                    for r in runs
                ],
                headers=["run_id", "仓库", "Issue", "状态", "当前步骤", "更新时间", "最后错误"],
            )
        )
        if not runs:
            click.echo("暂无运行。")

    @app.command("show")
    @click.option("--run", "run_id", required=True, help="运行 ID。")
    @click.pass_context
    def show_command(ctx, run_id):
        """展示运行、尝试、产物及外部操作。"""
        from dtcoder_agentic_dev.cli.app import runtime_for

        details = runtime_for(ctx).runs.details(run_id)
        click.echo(json.dumps(asdict(details), ensure_ascii=False, indent=2))

    def add_control(name, help_text):
        @click.command(name, help=help_text)
        @click.option("--run", "run_id", required=True, help="运行 ID。")
        @click.pass_context
        def command(ctx, run_id):
            from dtcoder_agentic_dev.cli.app import runtime_for

            runtime = runtime_for(ctx)
            if name == "retry":
                result = runtime.runs.retry(run_id)
            elif name == "cleanup":
                runtime.runs.cleanup(run_id)
                click.echo(f"已清理运行工作区：{run_id}")
                return
            else:
                result = runtime.runs.control(run_id, name)
            click.echo(f"运行 {result.run_id}：{result.status.value}")

        app.add_command(command)

    for name, text in [
        ("pause", "暂停运行，当前原子步骤完成后停止。"),
        ("resume", "恢复已暂停运行。"),
        ("cancel", "取消运行，保留现场。"),
        ("retry", "创建新的重跑记录，保留旧记录。"),
        ("cleanup", "清理已终止运行的隔离工作区。"),
    ]:
        add_control(name, text)
