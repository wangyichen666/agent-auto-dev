import click


def register(app):
    @app.command("run")
    @click.pass_context
    def run_command(ctx):
        """前台持续轮询；SIGINT/SIGTERM 后完成当前原子步骤并退出。"""
        from dtcoder_agentic_dev.cli.app import runtime_for

        runtime = runtime_for(ctx)
        if runtime.config.dry_run:
            click.echo("演练模式：请使用 run-once 查看待调度任务。")
            return
        runtime.scheduler.run_forever()

    @app.command("run-once")
    @click.pass_context
    def run_once_command(ctx):
        """轮询并执行一个调度周期，流水线等待会持久化后返回。"""
        from dtcoder_agentic_dev.cli.app import runtime_for

        runtime = runtime_for(ctx)
        if runtime.config.dry_run:
            click.echo(
                f"演练模式：当前有 {len(runtime.store.list_runs())} 个运行；不执行 Git 或 Codex。"
            )
            return
        results = runtime.scheduler.run_once()
        click.echo(f"调度完成：处理 {len(results)} 个运行。")
        if any(r.status.value == "FAILED" for r in results):
            raise click.ClickException("部分运行失败，请使用 show 查看详情")
        if runtime.scheduler.poller.last_errors:
            raise click.ClickException("部分仓库轮询失败，请检查代码托管能力和 scheduler.log")

    @app.command("process")
    @click.option("--repo", required=True, help="仓库名称或 ID。")
    @click.option("--issue", type=click.IntRange(min=1), required=True, help="Issue 编号。")
    @click.pass_context
    def process_command(ctx, repo, issue):
        """为指定 Issue 创建新运行，并执行一个调度周期。"""
        from dtcoder_agentic_dev.cli.app import runtime_for

        runtime = runtime_for(ctx)
        run = runtime.runs.process(repo, issue)
        if not runtime.config.dry_run:
            run = runtime.dispatcher.dispatch_once(run.run_id) or run
        click.echo(f"运行 {run.run_id}：{run.status.value}")
        if run.last_error:
            click.echo(f"错误：{run.last_error}")
        if run.status.value == "FAILED":
            raise click.ClickException("运行失败")
