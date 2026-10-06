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
                        names.get(r.repository_id, r.repository_id or "无仓"),
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
        @click.option("--feedback", help="YAML 任务的人工反馈（持久化前脱敏）。")
        @click.option(
            "--mode",
            type=click.Choice(["revise", "continue_conversation"]),
            default="revise",
            help="YAML 恢复方式；会话续聊尚未装配时明确拒绝。",
        )
        @click.pass_context
        def command(ctx, run_id, feedback, mode):
            from dtcoder_agentic_dev.cli.app import runtime_for

            runtime = runtime_for(ctx)
            if name not in {"resume", "retry", "skip"} and (
                feedback is not None or mode != "revise"
            ):
                raise click.ClickException("反馈与恢复方式只适用于 resume/retry/skip")
            if name == "retry":
                result = runtime.runs.retry(run_id, feedback=feedback, mode=mode)
            elif name == "cleanup":
                runtime.runs.cleanup(run_id)
                click.echo(f"已清理运行工作区：{run_id}")
                return
            else:
                result = runtime.runs.control(run_id, name, feedback=feedback, mode=mode)
            click.echo(f"运行 {result.run_id}：{result.status.value}")

        app.add_command(command)

    for name, text in [
        ("pause", "暂停运行，当前原子步骤完成后停止。"),
        ("resume", "恢复已暂停运行。"),
        ("cancel", "取消运行，保留现场。"),
        ("retry", "旧 Issue 创建重跑记录；YAML 从失败节点恢复并保留历史。"),
        ("cleanup", "清理已终止运行的隔离工作区。"),
        ("skip", "跳过已暂停且显式允许跳过的 YAML 节点。"),
    ]:
        add_control(name, text)

    @app.command("rollback")
    @click.option("--run", "run_id", required=True, help="原运行 ID。")
    @click.option(
        "--step",
        required=True,
        type=click.Choice(["requirements", "coding", "review", "fix", "pipeline", "create_pr"]),
    )
    @click.option("--reason", required=True, help="回退原因，写入审计。")
    @click.option("--dry-run", "plan_only", is_flag=True, help="只展示计划。")
    @click.option("--yes", is_flag=True, help="确认执行已展示的回退计划。")
    @click.option("--keep-pr", is_flag=True, help="保留未合并 PR。")
    @click.option("--keep-pipeline", is_flag=True, help="保留运行中的流水线。")
    @click.option("--no-backup", is_flag=True, help="关闭备份，需要再次显式确认。")
    @click.option("--confirm-no-backup", is_flag=True, help="再次确认不创建备份。")
    @click.pass_context
    def rollback_command(
        ctx,
        run_id,
        step,
        reason,
        plan_only,
        yes,
        keep_pr,
        keep_pipeline,
        no_backup,
        confirm_no_backup,
    ):
        """从节点输入修订创建后继运行，保留原尝试和审计。"""
        from dtcoder_agentic_dev.application.services.rollback import RollbackService
        from dtcoder_agentic_dev.cli.app import runtime_for

        service = RollbackService(runtime_for(ctx).runs)
        options = dict(keep_pr=keep_pr, keep_pipeline=keep_pipeline, backup=not no_backup)
        plan = service.plan(run_id, step, reason, **options)
        click.echo(json.dumps(plan, ensure_ascii=False, indent=2))
        if plan_only or not yes:
            click.echo("仅展示计划；执行需要 --yes。")
            return
        if no_backup and not confirm_no_backup:
            raise click.ClickException("关闭备份还需要 --confirm-no-backup")
        result = service.execute(
            run_id, step, reason, expected_revision=plan["source_revision"], **options
        )
        click.echo(f"已创建后继运行：{result.run_id}")

    @app.command("rollback-recover")
    @click.option("--run", "run_id", required=True)
    @click.option("--reason", required=True)
    @click.option("--yes", is_flag=True, help="确认恢复中断的回退审计。")
    @click.pass_context
    def rollback_recover(ctx, run_id, reason, yes):
        """恢复 PENDING 回退；存在后继时准备原目标工作区，否则记录中断。"""
        from dtcoder_agentic_dev.application.services.rollback import RollbackService
        from dtcoder_agentic_dev.cli.app import runtime_for

        if not yes:
            raise click.ClickException("恢复回退需要 --yes；先使用 show 检查 PENDING 操作")
        operation = RollbackService(runtime_for(ctx).runs).recover(run_id, reason)
        click.echo(f"回退审计 {operation.operation_id}：{operation.status.value}")
