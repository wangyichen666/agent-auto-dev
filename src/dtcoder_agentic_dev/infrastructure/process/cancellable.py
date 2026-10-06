"""可取消的参数数组进程；只终止本次 Popen 创建的独立进程组。"""

import os
import queue
import signal
import subprocess
import threading
import time

from dtcoder_agentic_dev.domain.errors import (
    AgentCancelled,
    ExternalCommandError,
    ExternalCommandTimeout,
)
from dtcoder_agentic_dev.infrastructure.process.command import SubprocessCommandRunner
from dtcoder_agentic_dev.ports.command_runner import CommandResult


class CancellableCommandRunner(SubprocessCommandRunner):
    def run_cancellable(
        self,
        args,
        *,
        cwd=None,
        stdin=None,
        timeout=300,
        check=True,
        cancel_requested=None,
        on_stdout=None,
        on_process=None,
        max_output_bytes=16 * 1024 * 1024,
    ):
        import math

        if (
            isinstance(args, (str, bytes))
            or not args
            or any(not isinstance(a, str) or "\x00" in a for a in args)
        ):
            raise ValueError("命令必须是非空字符串参数数组")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("命令超时必须大于 0")
        if cancel_requested and cancel_requested():
            raise AgentCancelled("模型执行已取消")
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                list(args),
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise ExternalCommandError("无法启动模型命令，请检查 binary 和工作目录") from exc
        if on_process:
            try:
                on_process(
                    {
                        "pid": process.pid,
                        "process_group": process.pid if os.name == "posix" else None,
                    }
                )
            except Exception:
                self._stop(process)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()
                raise
        chunks = queue.Queue(maxsize=128)
        outputs = {"stdout": [], "stderr": []}

        def reader(pipe, name):
            try:
                while data := os.read(pipe.fileno(), 4096):
                    chunks.put((name, data))
            finally:
                chunks.put((name, None))

        def writer():
            try:
                process.stdin.write((stdin or "").encode("utf-8"))
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                process.stdin.close()

        readers = [
            threading.Thread(target=reader, args=(pipe, name), daemon=True)
            for pipe, name in [(process.stdout, "stdout"), (process.stderr, "stderr")]
        ]
        input_thread = threading.Thread(target=writer, daemon=True)
        for thread in [*readers, input_thread]:
            thread.start()
        ended, size, failure = set(), 0, None
        try:
            while len(ended) < 2 or process.poll() is None:
                if failure is None:
                    if cancel_requested and cancel_requested():
                        failure = AgentCancelled("模型执行已取消")
                    elif time.monotonic() - started >= timeout:
                        failure = ExternalCommandTimeout("模型执行超时")
                    if failure:
                        self._stop(process)
                try:
                    name, data = chunks.get(timeout=0.02)
                except queue.Empty:
                    continue
                if data is None:
                    ended.add(name)
                    continue
                size += len(data)
                if size > max_output_bytes:
                    if failure is None:
                        failure = ExternalCommandError("模型输出超过大小限制")
                        self._stop(process)
                    continue
                outputs[name].append(data)
                if name == "stdout" and on_stdout and failure is None:
                    on_stdout(data)
            process.wait()
        finally:
            self._stop(process)
            # 消费队列，避免异常回调时 reader 卡在有界队列中。
            while any(thread.is_alive() for thread in readers):
                try:
                    chunks.get(timeout=0.02)
                except queue.Empty:
                    pass
            for thread in [*readers, input_thread]:
                thread.join(timeout=1)
            process.stdout.close()
            process.stderr.close()
        stdout, stderr = [
            b"".join(outputs[name]).decode("utf-8", errors="replace")
            for name in ("stdout", "stderr")
        ]
        duration = time.monotonic() - started
        if failure:
            failure.stdout, failure.stderr, failure.duration_seconds = stdout, stderr, duration
            raise failure
        if check and process.returncode:
            raise ExternalCommandError(
                "模型命令失败",
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration,
            )
        return CommandResult(tuple(args), process.returncode, stdout, stderr, duration)

    @staticmethod
    def _stop(process):
        # POSIX 子进程可能留下仍持有管道的后代，必须清理专属进程组。
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()
        process.wait()
