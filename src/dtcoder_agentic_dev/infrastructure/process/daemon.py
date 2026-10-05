"""POSIX 后台进程管理：PID + 启动时间 + 随机进程标识，拒绝 PID 复用。"""

import json
import os
import secrets
import shlex
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from dtcoder_agentic_dev.domain.errors import ConfigurationError, TechnicalError
from dtcoder_agentic_dev.infrastructure.filesystem.atomic import atomic_write
from dtcoder_agentic_dev.infrastructure.locking.file import file_lock


class DaemonManager:
    def __init__(self, config, config_path, commands, *, launch=None, kill=None, sleep=None):
        self.config_path = str(Path(os.path.expandvars(str(config_path))).expanduser().resolve())
        self.pid_file = Path(config.state.directory) / "scheduler.pid"
        self.log_file = Path(config.state.directory) / "logs/scheduler.log"
        self.commands = commands
        self.launch, self.kill, self.sleep = (
            launch or subprocess.Popen,
            kill or os.kill,
            sleep or time.sleep,
        )

    def _supported(self):
        if os.name != "posix":
            raise ConfigurationError("后台命令仅支持 macOS/Linux；Windows 请使用前台 run")

    def _identity(self, pid):
        result = self.commands.run(
            ["ps", "-ww", "-p", str(pid), "-o", "lstart=", "-o", "command="],
            timeout=10,
            check=False,
            env={"LC_ALL": "C", "TZ": "UTC"},
        )
        if result.returncode or not result.stdout.strip():
            return None
        # lstart 固定为五个字段；后续为完整参数数组的文本表示。
        fields = result.stdout.strip().split(maxsplit=5)
        if len(fields) != 6:
            return None
        return " ".join(fields[:5]), fields[5]

    def _status(self):
        if not self.pid_file.exists():
            return {"status": "停止"}
        try:
            record = json.loads(self.pid_file.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise ValueError
            if type(record.get("pid")) is not int or record["pid"] <= 1:
                raise ValueError
            current = self._identity(record["pid"])
            if not current or current[0] != record["started"]:
                return {"status": "stale PID"}
            args = shlex.split(current[1])
            token_index = args.index("--daemon-token")
            if (
                args[token_index + 1] != record["token"]
                or "-m" not in args
                or args[args.index("-m") + 1] != "dtcoder_agentic_dev.cli.app"
                or "dtcoder_agentic_dev.cli.app" not in args
                or "run" not in args
                or self.config_path != record["config"]
            ):
                return {"status": "stale PID"}
            return {"status": "运行", "pid": record["pid"]}
        except (ValueError, KeyError, IndexError, OSError):
            return {"status": "stale PID"}

    def status(self):
        self._supported()
        with file_lock(self.pid_file.with_suffix(".lock")):
            return self._status()

    def start(self):
        self._supported()
        with file_lock(self.pid_file.with_suffix(".lock")):
            if self._status()["status"] == "运行":
                raise ConfigurationError("后台调度器已运行")
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            token = secrets.token_hex(24)
            args = [
                sys.executable,
                "-m",
                "dtcoder_agentic_dev.cli.app",
                "--config",
                self.config_path,
                "run",
                "--daemon-token",
                token,
            ]
            with self.log_file.open("ab") as log:
                process = self.launch(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    shell=False,
                )
            try:
                current = None
                for _ in range(20):
                    if process.poll() is not None:
                        raise TechnicalError("后台进程启动失败，请检查日志")
                    current = self._identity(process.pid)
                    if current and token in current[1]:
                        break
                    self.sleep(0.05)
                if not current or token not in current[1]:
                    raise TechnicalError("无法确认后台进程身份")
                record = {
                    "pid": process.pid,
                    "started": current[0],
                    "token": token,
                    "config": self.config_path,
                }
                atomic_write(self.pid_file, json.dumps(record))
                self.sleep(0.1)
                if process.poll() is not None:
                    self.pid_file.unlink(missing_ok=True)
                    raise TechnicalError("后台进程提前退出，请检查日志")
                return {"status": "运行", "pid": process.pid}
            except Exception:
                # 这里持有新启动的进程对象，只终止仍属于该对象的子进程。
                if process.poll() is None:
                    process.terminate()
                raise

    def stop(self, timeout=30, *, force=False):
        self._supported()
        if timeout <= 0:
            raise ConfigurationError("停止超时必须大于 0")
        with file_lock(self.pid_file.with_suffix(".lock")):
            status = self._status()
            if status["status"] != "运行":
                self.pid_file.unlink(missing_ok=True)
                return status
            pid = status["pid"]
            # 每次信号前重新核验进程身份；stale PID 永不发送信号。
            if self._status() != status:
                raise TechnicalError("进程身份发生变化，拒绝发送信号")
            try:
                self.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                self.pid_file.unlink(missing_ok=True)
                return {"status": "停止"}
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._status()["status"] != "运行":
                    self.pid_file.unlink(missing_ok=True)
                    return {"status": "停止"}
                self.sleep(min(0.2, timeout))
            if not force:
                raise TechnicalError(
                    "SIGTERM 等待超时；进程可能在完成原子步骤，使用 --force 明确允许 SIGKILL"
                )
            if self._status() == status:
                self.kill(pid, signal.SIGKILL)
            # 不先删除 PID：SIGKILL 尚未完成时仍能准确显示进程状态。
            return {"status": "已发送 SIGKILL"}

    def logs(self, lines=100, follow=False):
        if lines < 0:
            raise ConfigurationError("日志行数不能为负")
        if not self.log_file.is_file():
            raise ConfigurationError("调度器日志不存在，请先执行 init")
        with self.log_file.open(encoding="utf-8", errors="replace") as stream:
            yield "".join(deque(stream, maxlen=lines))
            if follow:
                while True:
                    text = stream.readline()
                    if text:
                        yield text
                    else:
                        # 支持日志截断；跟随由 Ctrl-C 终止。
                        if stream.tell() > self.log_file.stat().st_size:
                            stream.seek(0)
                        self.sleep(0.2)
