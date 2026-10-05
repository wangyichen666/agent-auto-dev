import signal
import threading


class SchedulerRunner:
    def __init__(self, poller, dispatcher, sleeper, poll_interval):
        self.poller, self.dispatcher, self.sleeper = poller, dispatcher, sleeper
        self.poll_interval = poll_interval
        self.stopping = False

    def stop(self, *args):
        self.stopping = True

    def run_once(self):
        self.poller.poll()
        results = []
        # 每个 run 在一个周期只调度一次，WAITING 和达到节点上限的任务留待下个周期。
        seen = set()
        while not self.stopping:
            eligible = [r for r in self.dispatcher.store.list_runs() if r.run_id not in seen]
            progressed = False
            for run in eligible:
                if self.stopping:
                    break
                seen.add(run.run_id)
                result = self.dispatcher.dispatch_once(run.run_id)
                if result:
                    results.append(result)
                    progressed = True
            if not progressed:
                break
        return results

    def run_forever(self):
        previous = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, self.stop)
        try:
            while not self.stopping:
                self.run_once()
                # 短等待使信号处理后及时退出；当前原子步骤自然完成。
                remaining = self.poll_interval
                while remaining > 0 and not self.stopping:
                    amount = min(remaining, 1)
                    self.sleeper.sleep(amount)
                    remaining -= amount
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
