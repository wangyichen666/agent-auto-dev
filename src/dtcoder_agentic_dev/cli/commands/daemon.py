"""后台命令无需建立工作流 runtime 或触发事件。"""

import click

from dtcoder_agentic_dev.config import load_config
from dtcoder_agentic_dev.infrastructure.process.command import SubprocessCommandRunner
from dtcoder_agentic_dev.infrastructure.process.daemon import DaemonManager


def register(app):
    def manager(ctx):
        return DaemonManager(
            load_config(ctx.obj["config_path"]), ctx.obj["config_path"], SubprocessCommandRunner()
        )

    @app.command("start")
    @click.pass_context
    def start(ctx):
        """后台启动，原子写入进程身份和 PID。"""
        click.echo(manager(ctx).start())

    @app.command("status")
    @click.pass_context
    def status(ctx):
        """检查运行、停止和 stale PID；核验进程启动身份。"""
        click.echo(manager(ctx).status())

    @app.command("stop")
    @click.option(
        "--timeout",
        type=click.FloatRange(min=0, min_open=True),
        default=30,
        help="SIGTERM 等待秒数。",
    )
    @click.option("--force", is_flag=True, help="超时后明确允许 SIGKILL。")
    @click.pass_context
    def stop(ctx, timeout, force):
        """优雅停止；默认超时后保留进程。"""
        click.echo(manager(ctx).stop(timeout, force=force))

    @app.command("restart")
    @click.option("--timeout", type=click.FloatRange(min=0, min_open=True), default=30)
    @click.pass_context
    def restart(ctx, timeout):
        """先优雅停止，再后台启动。"""
        daemon = manager(ctx)
        daemon.stop(timeout)
        click.echo(daemon.start())

    @app.command("logs")
    @click.option("--lines", type=click.IntRange(min=0), default=100, help="末尾行数。")
    @click.option("--follow", is_flag=True, help="持续跟随，Ctrl-C 退出。")
    @click.pass_context
    def logs(ctx, lines, follow):
        """查看后台调度器日志。"""
        try:
            for text in manager(ctx).logs(lines, follow):
                click.echo(text, nl=False)
        except KeyboardInterrupt:
            return
