import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


class WorkerTest(unittest.TestCase):
    def request(self, path="state", method="GET"):
        request = urllib.request.Request(f"http://127.0.0.1:8765/{path}", method=method)
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.load(response)

    def start(self, root):
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("worker.py"))],
            env={**os.environ, "TASK_ROOT": root},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop, process)
        for _ in range(100):
            if process.poll() is not None:
                self.fail(process.stderr.read().decode())
            try:
                return process, self.request()
            except urllib.error.URLError:
                time.sleep(0.02)
        self.fail("Worker readiness timed out")

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        if process.stderr:
            process.stderr.close()

    def test_live_worker_and_restart(self):
        with tempfile.TemporaryDirectory() as root:
            process, initial = self.start(root)
            self.assertEqual(initial["index"], 0)
            for index, total in enumerate([11, 34, 71, 112, 171], start=1):
                state = self.request("step", "POST")
                self.assertEqual(
                    state, {"token": initial["token"], "index": index, "total": total}
                )
                self.assertEqual((Path(root) / "result.json").exists(), index == 5)
            self.assertEqual(
                json.loads((Path(root) / "result.json").read_text()),
                {"count": 5, "sum": 171},
            )
            self.assertEqual(self.request("step", "POST"), state)
            self.stop(process)
            _, restarted = self.start(root)
            self.assertEqual(restarted["index"], 0)
            self.assertEqual(restarted["total"], 0)
            self.assertNotEqual(restarted["token"], initial["token"])


if __name__ == "__main__":
    unittest.main()
