import click
from tabulate import tabulate

from dtcoder_agentic_dev.application.services.diagnostics import diagnose
from dtcoder_agentic_dev.application.services.initialization import initialize


def register(app):
    @app.command("init")
    @click.pass_context
    def init_command(ctx):
        """初始化配置、数据库、模板和日志；保留已有文件。"""
        path = initialize(ctx.obj["config_path"])
        click.echo(f"初始化完成：{path}")

    @app.command("doctor")
    @click.pass_context
    def doctor_command(ctx):
        """检查配置、工具、模板、SQLite 和外部能力。"""
        from dtcoder_agentic_dev.cli.app import runtime_for

        diagnostics = diagnose(runtime_for(ctx))
        click.echo(
            tabulate(
                [[d.component, "通过" if d.ok else "未就绪", d.detail] for d in diagnostics],
                headers=["组件", "状态", "诊断"],
                tablefmt="simple",
            )
        )
        if not all(d.ok for d in diagnostics):
            raise click.ClickException("环境尚未就绪，请按诊断配置缺少的能力")
