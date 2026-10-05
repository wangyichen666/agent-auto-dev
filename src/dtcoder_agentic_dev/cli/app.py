import sqlite3

import click

from dtcoder_agentic_dev import __version__
from dtcoder_agentic_dev.cli.commands.configuration import register as register_configuration
from dtcoder_agentic_dev.cli.commands.execution import register as register_execution
from dtcoder_agentic_dev.cli.commands.runs import register as register_runs
from dtcoder_agentic_dev.cli.commands.setup import register as register_setup
from dtcoder_agentic_dev.config import DEFAULT_CONFIG
from dtcoder_agentic_dev.domain.errors import AgenticDevError


class ChineseContext(click.Context):
    def get_help(self):
        text = super().get_help()
        for source, target in [
            ("Usage:", "用法："),
            ("Options:", "选项："),
            ("Commands:", "命令："),
            ("Arguments:", "参数："),
            ("Show this message and exit.", "显示帮助并退出。"),
            ("default:", "默认："),
            ("required", "必填"),
        ]:
            text = text.replace(source, target)
        return text


class ChineseClickException(click.ClickException):
    def show(self, file=None):
        click.echo(f"错误：{self.format_message()}", file=file, err=True)


class ChineseGroup(click.Group):
    context_class = ChineseContext

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except (AgenticDevError, OSError, KeyError, ValueError, sqlite3.Error) as exc:
            raise ChineseClickException(f"执行失败：{exc}") from exc
        except click.ClickException as exc:
            raise ChineseClickException(exc.format_message()) from exc


@click.group(cls=ChineseGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=str),
    default=str(DEFAULT_CONFIG),
    help="配置 YAML 文件路径。",
    show_default=True,
)
@click.version_option(
    __version__,
    prog_name="dtcoder-agentic-dev",
    help="显示版本并退出。",
    message="%(prog)s 版本 %(version)s",
)
@click.pass_context
def app(ctx, config_path):
    """可恢复、可扩展的 Issue 自动研发工作流。"""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


def runtime_for(ctx):
    from dtcoder_agentic_dev.cli.bootstrap import build_runtime

    runtime = build_runtime(ctx.obj["config_path"])
    ctx.call_on_close(runtime.close)
    return runtime


register_setup(app)
register_execution(app)
register_runs(app)
register_configuration(app)


def _localize_commands(command):
    command.context_class = ChineseContext
    if isinstance(command, click.Group):
        for child in command.commands.values():
            _localize_commands(child)


_localize_commands(app)


def main():
    app()


if __name__ == "__main__":
    main()
