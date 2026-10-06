"""显式运行的真实 Claude CLI 验收，不纳入离线 pytest。

用法：.venv/bin/python scripts/e2e_claude.py --case basic
每次使用独立临时目录，保留脱敏产物与结果。将产生真实模型用量。
"""

import argparse
import json
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from threading import Thread

import yaml
from click.testing import CliRunner

from dtcoder_agentic_dev.application.services.initialization import initialize
from dtcoder_agentic_dev.cli.app import app
from dtcoder_agentic_dev.cli.bootstrap import build_runtime
from dtcoder_agentic_dev.domain.models import AttemptStatus, RunStatus
from dtcoder_agentic_dev.domain.security import redact
from dtcoder_agentic_dev.infrastructure.filesystem.atomic import atomic_write
from dtcoder_agentic_dev.infrastructure.process.ownership import process_is_alive


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def definition(prompt, marker, *, timeout=180):
    return yaml.safe_dump(
        {
            "name": "claude-live-e2e",
            "version": "1",
            "context": {"language": "zh"},
            "stages": ["write"],
            "jobs": {
                "write": {
                    "stage": "write",
                    "agent": "default",
                    "prompt": prompt,
                    "outputs": {"report": "report.md"},
                    "artifact_check": [
                        {"type": "nonempty", "path": "report.md"},
                        {"type": "regex", "path": "report.md", "pattern": marker},
                    ],
                    "config": {"execute": {"timeout": timeout, "retries": 0}},
                }
            },
        },
        allow_unicode=True,
        sort_keys=False,
    )


def main():
    parser = argparse.ArgumentParser(description="真实 Claude CLI 端到端验收，会产生模型用量")
    parser.add_argument(
        "--case", choices=["basic", "pause-resume", "cancel", "timeout"], default="basic"
    )
    parser.add_argument("--directory", type=Path, help="新的验收目录；默认创建临时目录并保留")
    parser.add_argument("--model", help="可选显式模型；默认使用本地 Claude 默认模型")
    args = parser.parse_args()
    root = (
        args.directory.resolve()
        if args.directory
        else Path(tempfile.mkdtemp(prefix="auto-dev-claude-e2e-"))
    )
    require(
        not root.exists() or (root.is_dir() and not any(root.iterdir())),
        "必须使用空目录，不能覆盖已有文件",
    )
    root.mkdir(parents=True, exist_ok=True)
    require(not (root / "config.yaml").exists(), "必须使用新的验收目录，不能覆盖旧配置")
    binary = shutil.which("claude")
    require(binary is not None, "未找到本地 claude")
    initialize(root / "config.yaml")
    config = yaml.safe_load((root / "config.yaml").read_text())
    config["claude"].update(binary=binary, timeout=180, max_turns=8, model=args.model or "")
    if args.case in {"pause-resume", "cancel"}:
        config["claude"]["allowed_tools"].append("Bash(sleep *)")
    (root / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True))
    marker = "E2E-" + uuid.uuid4().hex[:12]
    summary = {"case": args.case, "directory": str(root), "marker": marker, "status": "RUNNING"}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    runtime = build_runtime(root / "config.yaml", console_logging=False)
    worker = None
    run = None
    errors, results = [], []
    started = time.monotonic()
    try:
        if args.case == "basic":
            prompt = f"根据任务需求，用 Write 工具在当前工作区创建 report.md，包含标题、三条验证说明和标记 {marker}，全文不超过150字。只读写本工作区，不执行命令、不访问网络。最终仅说完成。"
            source = root / "workflow.yaml"
            source.write_text(definition(prompt, marker), encoding="utf-8")
            invoked = CliRunner().invoke(
                app,
                [
                    "--config",
                    str(root / "config.yaml"),
                    "submit",
                    "--workflow",
                    str(source),
                    "--task",
                    "用中文说明：任务提交、模型执行、产物校验分别验证什么。",
                ],
            )
            summary["cli_exit_code"] = invoked.exit_code
            runs = runtime.store.list_runs()
            run = runs[0] if runs else None
            require(invoked.exit_code == 0, "真实 CLI submit 失败：" + redact(invoked.output))
            require(run is not None, "CLI 没有创建运行记录")
            result = runtime.store.load_run(run.run_id)
            require(result.status is RunStatus.SUCCEEDED, f"真实任务失败：{result.last_error}")
        elif args.case == "timeout":
            run = runtime.declarative.submit(
                definition("请创建 report.md，详细写一篇说明。", marker, timeout=0.2),
                "验证真实进程超时",
            )
            result = runtime.dispatcher.dispatch_once(run.run_id)
            require(result.status is RunStatus.FAILED, "超时没有映射为 FAILED")
            require(
                runtime.store.list_attempts(run.run_id)[0].error_code == "TIMEOUT",
                "错误码不是 TIMEOUT",
            )
        else:
            prompt = f"严格依次执行：1. 用 Write 创建 draft.md，内容为 {marker}。2. 用 Bash 执行唯一的命令 sleep 15，等待其完成。3. 用 Write 创建 report.md，包含标记 {marker} 和中文验证说明。禁止执行其他命令，禁止访问网络、工作区外文件或凭据。如果人工反馈要求结束等待，应优先执行反馈。"
            run = runtime.declarative.submit(
                definition(prompt, marker), "验证可持久化的暂停、续聊和取消。"
            )

            def dispatch():
                try:
                    results.append(runtime.dispatcher.dispatch_once(run.run_id))
                except Exception as exc:
                    errors.append(exc)

            worker = Thread(target=dispatch, name="claude-e2e-dispatch")
            worker.start()
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                current = runtime.store.load_run(run.run_id)
                if current.workspace_path and (Path(current.workspace_path) / "draft.md").exists():
                    break
                if not worker.is_alive():
                    raise AssertionError(
                        f"模型未生成暂停触发产物：{current.status.value} / {current.last_error}"
                    )
                time.sleep(0.1)
            else:
                raise AssertionError("等待真实模型 draft.md 超时")
            time.sleep(0.2)
            action = "pause" if args.case == "pause-resume" else "cancel"
            runtime.runs.control(run.run_id, action)
            worker.join(10)
            require(not worker.is_alive() and not errors, "控制后进程未回收或发生异常")
            result = runtime.store.load_run(run.run_id)
            expected = RunStatus.PAUSED if action == "pause" else RunStatus.CANCELLED
            require(result.status is expected, f"控制状态不正确：{result.status.value}")
            first = runtime.store.list_attempts(run.run_id)[0]
            require(
                first.status
                is (AttemptStatus.PAUSED if action == "pause" else AttemptStatus.CANCELLED),
                "attempt 状态与控制意图不一致",
            )
            require(first.metadata.get("session_id"), "真实 session 没有持久化")
            require(not process_is_alive(first.metadata["process"]), "当前任务进程组仍存活")
            summary["controlled_attempt"] = {
                "status": first.status.value,
                "session_id": first.metadata["session_id"],
            }
            if action == "pause":
                runtime.runs.control(
                    run.run_id,
                    "resume",
                    mode="continue_conversation",
                    feedback=f"人工检查已结束，立即跳过等待，创建 report.md；包含 {marker} 和‘人工续聊完成’，并说明暂停后如何继续。不要执行 Bash。",
                )
                result = runtime.dispatcher.dispatch_once(run.run_id)
                require(result.status is RunStatus.SUCCEEDED, f"真实续聊失败：{result.last_error}")
                attempts = [
                    a for a in runtime.store.list_attempts(run.run_id) if a.step_name == "write"
                ]
                require(len(attempts) == 2, "续聊没有创建独立 attempt")
                require(
                    attempts[1].metadata["session_id"] == first.metadata["session_id"],
                    "续聊未复用 session",
                )
                require(
                    "人工续聊完成" in (Path(result.workspace_path) / "report.md").read_text(),
                    "产物没有体现人工反馈",
                )
        attempts = runtime.store.list_attempts(run.run_id)
        summary.update(
            status="PASSED",
            run_id=run.run_id,
            run_status=result.status.value,
            duration_seconds=round(time.monotonic() - started, 3),
            attempts=[
                {
                    "number": a.attempt_number,
                    "job": a.step_name,
                    "status": a.status.value,
                    "engine": a.metadata.get("engine"),
                    "session_id": a.metadata.get("session_id"),
                    "error_code": a.error_code,
                }
                for a in attempts
            ],
        )
        require(runtime.store.load_run(run.run_id).lease_owner is None, "任务结束后租约未释放")
    except Exception as exc:
        summary.update(status="FAILED", error=redact(f"{type(exc).__name__}：{exc}"))
        if run:
            summary["run_id"] = run.run_id
        raise
    finally:
        if worker and worker.is_alive() and run:
            current = runtime.store.load_run(run.run_id)
            if current.status in {
                RunStatus.RUNNING,
                RunStatus.QUEUED,
                RunStatus.PAUSED,
                RunStatus.WAITING,
            }:
                runtime.runs.control(run.run_id, "cancel")
            worker.join(10)
        runtime.close()
        atomic_write(
            root / "summary.json", json.dumps(redact(summary), ensure_ascii=False, indent=2)
        )
        print(json.dumps(redact(summary), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
