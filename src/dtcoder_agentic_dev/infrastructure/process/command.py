"""仅使用参数数组；不记录环境变量、stdin 或完整命令参数。"""

import logging
import math
import os
import subprocess
import time
from typing import Mapping, Sequence

from dtcoder_agentic_dev.domain.errors import ExternalCommandError, ExternalCommandTimeout
from dtcoder_agentic_dev.ports.command_runner import CommandResult


class SubprocessCommandRunner:
    def __init__(self, monotonic=time.monotonic):
        self.monotonic = monotonic
        self.logger = logging.getLogger(__name__)

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str | None = None,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 300,
        check: bool = True,
    ) -> CommandResult:
        if isinstance(args, (str, bytes)) or not args or any(not isinstance(a, str) for a in args):
            raise ValueError("命令必须是非空字符串参数数组")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("命令超时必须大于 0")
        started = self.monotonic()
        merged_env = dict(os.environ)
        if env:
            merged_env.update(env)
        try:
            result = subprocess.run(
                list(args),
                cwd=cwd,
                input=stdin,
                env=merged_env,
                timeout=timeout,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:

            def decode(output):
                return (
                    output.decode("utf-8", errors="replace")
                    if isinstance(output, bytes)
                    else output or ""
                )

            raise ExternalCommandTimeout(
                f"外部命令执行超时（{timeout} 秒）",
                stdout=decode(exc.stdout),
                stderr=decode(exc.stderr),
                duration_seconds=self.monotonic() - started,
            ) from exc
        except OSError as exc:
            raise ExternalCommandError(
                "无法启动外部命令，请检查 binary 和工作目录",
                duration_seconds=self.monotonic() - started,
            ) from exc
        duration = self.monotonic() - started
        self.logger.debug("外部命令结束，退出码=%s，耗时=%.3f 秒", result.returncode, duration)
        command_result = CommandResult(
            tuple(args), result.returncode, result.stdout, result.stderr, duration
        )
        if check and result.returncode != 0:
            raise ExternalCommandError(
                f"外部命令失败，退出码 {result.returncode}",
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_seconds=duration,
            )
        return command_result
