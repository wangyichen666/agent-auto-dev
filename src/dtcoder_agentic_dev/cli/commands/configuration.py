import click
import yaml

from dtcoder_agentic_dev.config import load_config, redacted_config


def register(app):
    @app.group("config")
    def config_group():
        """展示配置。"""

    @config_group.command("show")
    @click.pass_context
    def show_command(ctx):
        """显示配置；隐藏可能包含凭据的命令参数。"""
        click.echo(
            yaml.safe_dump(
                redacted_config(load_config(ctx.obj["config_path"])),
                allow_unicode=True,
                sort_keys=False,
            )
        )
