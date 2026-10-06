import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from run import Run


class StateEvidenceTest(unittest.TestCase):
    def verify(self, run, state, marker, result_exists, expected, expected_marker):
        vm = Mock(id="checkpoint-vm")
        vm.files.read_text.return_value = marker
        vm.commands.run.side_effect = [
            Mock(stdout=json.dumps(state), stderr="", exit_code=0),
            Mock(stdout=json.dumps(result_exists), stderr="", exit_code=0),
        ]
        return run.verify_state(vm, "checkpoint restored", expected, expected_marker)

    def test_intermediate_states_survive_completion_and_are_hashed(self):
        midpoint = {"token": "original-memory-token", "index": 2, "total": 34}
        initial = {**midpoint, "index": 0, "total": 0}
        with tempfile.TemporaryDirectory() as root:
            run = Run.start(Path(root))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.verify(run, midpoint, "midpoint\n", False, midpoint, "midpoint\n")
                self.verify(run, initial, "starting-state\n", False, initial, "starting-state\n")
                run.manifest["status"] = "passed"
                run.finalize()
            states = json.loads((run.output / "states.json").read_text())
            self.assertEqual([item["state"]["index"] for item in states], [2, 0])
            self.assertTrue(all(item["passed"] for item in states))
            self.assertIn("step=2 sum=34", output.getvalue())
            self.assertIn("step=0 sum=0", output.getvalue())
            summary = (run.output / "summary.md").read_text()
            self.assertIn("| 2 | 34 |", summary)
            self.assertIn("| 0 | 0 |", summary)
            manifest = json.loads((run.output / "manifest.json").read_text())
            self.assertIn("states.json", manifest["sha256"])
            self.assertIn("summary.md", manifest["sha256"])

    def test_wrong_memory_disk_or_later_result_saves_failure_before_raising(self):
        expected = {"token": "original-memory-token", "index": 2, "total": 34}
        cases = [
            ({**expected, "token": "restarted-token"}, "midpoint\n", False),
            ({**expected, "index": 5, "total": 171}, "midpoint\n", False),
            (expected, "starting-state\n", False),
            (expected, "midpoint\n", True),
        ]
        for state, marker, result_exists in cases:
            with self.subTest(state=state, marker=marker, result_exists=result_exists):
                with tempfile.TemporaryDirectory() as root:
                    run = Run.start(Path(root))
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        with self.assertRaisesRegex(RuntimeError, "State check failed"):
                            self.verify(run, state, marker, result_exists, expected, "midpoint\n")
                    record = json.loads((run.output / "states.json").read_text())[0]
                    self.assertFalse(record["passed"])
                    self.assertEqual(record["state"], state)
                    self.assertEqual(record["expected"]["state"], expected)
                    self.assertEqual(record["result_exists"], result_exists)
                    self.assertIn("[FAIL]", output.getvalue())
                    self.assertIn("| FAIL |", (run.output / "summary.md").read_text())


if __name__ == "__main__":
    unittest.main()
