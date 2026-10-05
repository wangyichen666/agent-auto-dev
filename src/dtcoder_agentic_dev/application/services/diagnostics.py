from pathlib import Path

from dtcoder_agentic_dev.application.dto import Diagnostic


def diagnose(runtime):
    results = [Diagnostic("配置", True, "YAML 与字段校验通过")]
    try:
        with runtime.store.transaction():
            runtime.store.connection.execute("SELECT version FROM schema_version").fetchone()
        results.append(Diagnostic("SQLite", True, "schema 版本 1，事务正常"))
    except Exception as exc:
        results.append(Diagnostic("SQLite", False, f"数据库检查失败：{type(exc).__name__}"))
    for name, binary in [
        ("Git", runtime.config.git.binary),
        ("Codex", runtime.config.codex.binary),
    ]:
        try:
            runtime.commands.run([binary, "--version"], timeout=10)
            results.append(Diagnostic(name, True, "命令可用；认证由外部工具管理"))
        except Exception as exc:
            results.append(Diagnostic(name, False, f"命令不可用：{type(exc).__name__}"))
    try:
        path = Path(runtime.config.state.directory) / ".doctor-write-check"
        with path.open("x", encoding="utf-8") as stream:
            stream.write("目录可写")
        path.unlink()
        results.append(Diagnostic("状态目录", True, "可读写"))
    except OSError:
        results.append(Diagnostic("状态目录", False, "目录不可写或检查文件已存在"))
    missing = [
        name
        for name in ("requirements", "coding", "code_review", "fix_loop", "pr_create")
        if not (Path(runtime.config.prompts.directory) / f"{name}.txt").is_file()
    ]
    results.append(
        Diagnostic("模板", not missing, "完整" if not missing else "缺少模板：" + ",".join(missing))
    )
    host_configured = runtime.config.dry_run or runtime.config.code_host.adapter != "logging"
    results.append(
        Diagnostic(
            "代码托管/认证",
            host_configured,
            "dry-run 演练模式，无真实认证"
            if runtime.config.dry_run
            else "已装配外部适配器；离线检查不验证真实认证"
            if host_configured
            else "未配置真实 CodeHost 适配器，不能获取 Issue 或创建 PR",
        )
    )
    results.append(
        Diagnostic(
            "流水线",
            not any(r.pipeline_enabled for r in runtime.config.repositories),
            "已禁用"
            if not any(r.pipeline_enabled for r in runtime.config.repositories)
            else "需要注入适配器",
        )
    )
    results.append(
        Diagnostic(
            "仓库",
            bool(runtime.config.repositories),
            f"已配置 {len(runtime.config.repositories)} 个仓库"
            if runtime.config.repositories
            else "未配置仓库，请编辑 config.yaml",
        )
    )
    for repo in runtime.config.repositories:
        if repo.path:
            ok = runtime.git.is_repository(repo.path)
            results.append(
                Diagnostic(f"仓库 {repo.name}", ok, "本地 Git 仓库有效" if ok else "不是 Git 仓库")
            )
    return results
