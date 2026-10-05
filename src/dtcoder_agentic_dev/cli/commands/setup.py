import click
from tabulate import tabulate

from dtcoder_agentic_dev.application.services.diagnostics import diagnose
from dtcoder_agentic_dev.application.services.initialization import initialize


def register(app):
    @app.command("init")
    @click.option("--name", help="仓库显示名称。")
    @click.option("--path", help="本地源仓库路径，只作为镜像源。")
    @click.option("--remote", help="远端 URL，不得带凭据。")
    @click.option("--project", help="平台项目 ID。")
    @click.option("--provider", help="平台标识。")
    @click.option("--base-branch", help="基础分支。")
    @click.option("--label", "labels", multiple=True, help="Issue 标签，可重复。")
    @click.option("--author", "authors", multiple=True, help="Issue 作者 ID，可重复。")
    @click.option("--reviewer", "reviewers", multiple=True, help="PR reviewer ID，可重复。")
    @click.option("--remove-source-branch/--keep-source-branch", default=None)
    @click.option("--pipeline-enabled/--pipeline-disabled", default=None)
    @click.option("--pipeline-project")
    @click.option("--yml-path")
    @click.option("--template-id")
    @click.option("--yaml-file")
    @click.option("--yml-global-path")
    @click.option("--pipeline-branch")
    @click.option("--param", "params", multiple=True, help="ACI 参数 key=value，可重复。")
    @click.option("--env-file")
    @click.option("--source")
    @click.option("--code-host-adapter", type=click.Choice(["logging", "antcode"]))
    @click.option("--pipeline-adapter", type=click.Choice(["disabled", "aci"]))
    @click.option("--update-templates", is_flag=True, help="更新内置模板，覆盖前备份。")
    @click.option("--prepare-mirror", is_flag=True, help="预先准备受管镜像。")
    @click.pass_context
    def init_command(ctx, update_templates, prepare_mirror, **options):
        """初始化或按稳定身份更新仓库；保留用户模板。"""
        repository = {k: v for k, v in options.items() if v is not None and v != ()}
        ci = {}
        for name in (
            "pipeline_project",
            "yml_path",
            "template_id",
            "yaml_file",
            "yml_global_path",
            "pipeline_branch",
            "params",
            "env_file",
            "source",
        ):
            if name in repository:
                value = repository.pop(name)
                ci[{"pipeline_project": "project", "pipeline_branch": "branch"}.get(name, name)] = (
                    value
                )
        if "params" in ci:
            pairs = {}
            for param in ci["params"]:
                if "=" not in param:
                    raise click.ClickException("--param 必须使用 key=value")
                key, value = param.split("=", 1)
                pairs[key] = value
            ci["params"] = pairs
        if ci:
            repository["pipeline"] = ci
        for name in ("labels", "authors", "reviewers"):
            if name in repository:
                repository[name] = list(repository[name])
        path = initialize(
            ctx.obj["config_path"],
            repository=repository,
            update_templates=update_templates,
            prepare_mirror=prepare_mirror,
        )
        click.echo(f"初始化完成：{path}")

    @app.command("doctor")
    @click.pass_context
    def doctor_command(ctx):
        """只读检查工具、登录、模板、SQLite 与仓库远端。"""
        from dtcoder_agentic_dev.cli.app import runtime_for

        diagnostics = diagnose(runtime_for(ctx))
        click.echo(
            tabulate(
                [
                    [d.component, d.status or ("通过" if d.ok else "未就绪"), d.detail]
                    for d in diagnostics
                ],
                headers=["组件", "状态", "诊断"],
            )
        )
        if not all(d.ok for d in diagnostics):
            raise click.ClickException("环境尚未就绪，请按诊断配置缺少的能力")
