"""Run against real Superserve VMs; export evidence even if a check fails."""

import argparse
import hashlib
import json
import os
import shlex
import textwrap
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from superserve import Sandbox, __version__

TASK_ROOT = "/tmp/lifecycle-task"
EXPECTED_RESULT = {"count": 5, "sum": 171}

START_WORKER = f"nohup python3 {TASK_ROOT}/worker.py >{TASK_ROOT}/worker.log 2>&1 </dev/null &"

# Polls the worker's HTTP endpoint until it responds, instead of sleeping a fixed duration.
WAIT_FOR_WORKER = textwrap.dedent("""\
    import time, urllib.request
    for _ in range(50):
        try:
            urllib.request.urlopen("http://127.0.0.1:8765/state", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("worker did not start")
    """)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def worker_request(path, method):
    """Python one-liner, run inside the sandbox, that calls the worker's HTTP API."""
    return (
        "import urllib.request; print(urllib.request.urlopen("
        f"urllib.request.Request('http://127.0.0.1:8765/{path}', method={method!r}), "
        "timeout=5).read().decode())"
    )


@dataclass
class Run:
    """One invocation: creates sandboxes/snapshots, records events, writes evidence on exit."""

    run_id: str
    output: Path
    manifest: dict
    sandboxes: list = field(default_factory=list)
    snapshots: list = field(default_factory=list)

    @classmethod
    def start(cls, output_root: Path) -> "Run":
        run_id = uuid.uuid4().hex[:12]
        output = output_root / run_id
        output.mkdir(parents=True)
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "sdk_version": __version__,
            "status": "running",
            "sandboxes": [],
            "snapshots": [],
            "scores": {},
        }
        return cls(run_id=run_id, output=output, manifest=manifest)

    def event(self, kind, **data):
        with (self.output / "events.jsonl").open("a") as stream:
            stream.write(
                json.dumps({"time": datetime.now(timezone.utc).isoformat(), "kind": kind, **data}) + "\n"
            )

    def lifecycle(self, operation, call):
        start = time.monotonic()
        self.event("lifecycle_start", operation=operation)
        result = call()
        self.event("lifecycle_end", operation=operation, duration_seconds=time.monotonic() - start)
        return result

    def create_sandbox(self, label, **kwargs):
        vm = self.lifecycle(
            f"create:{label}",
            lambda: Sandbox.create(
                name=f"repro-{self.run_id}-{label}",
                timeout_seconds=300,
                auto_delete_seconds=3600,
                **kwargs,
            ),
        )
        self.sandboxes.append(vm)
        self.manifest["sandboxes"].append({"label": label, "id": vm.id})
        return vm

    def take_snapshot(self, vm, label):
        snap = self.lifecycle(
            f"snapshot:{label}",
            lambda: vm.snapshot(
                name=f"repro-{self.run_id}-{label}", idempotency_key=f"{self.run_id}-{label}"
            ),
        )
        self.snapshots.append(snap)
        self.manifest["snapshots"].append({"label": label, "id": snap.id})
        return snap

    def run_command(self, vm, text):
        self.event("action", sandbox_id=vm.id, command=text)
        result = vm.commands.run(text, timeout_seconds=30)
        self.event(
            "observation",
            sandbox_id=vm.id,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
        )
        require(result.exit_code == 0, f"Command failed in {vm.id}: {result.stderr}")
        return result.stdout

    def task_state(self, vm):
        return json.loads(self.run_command(vm, "python3 -c " + shlex.quote(worker_request("state", "GET"))))

    def step_task(self, vm):
        return json.loads(self.run_command(vm, "python3 -c " + shlex.quote(worker_request("step", "POST"))))

    def finish_task(self, vm, label):
        """Drive the task to completion, grade its output, and export the evidence."""
        current = self.task_state(vm)
        for _ in range(5 - current["index"]):
            self.step_task(vm)
        artifact = vm.files.read(TASK_ROOT + "/result.json")
        score = int(json.loads(artifact) == EXPECTED_RESULT)
        self.manifest["scores"][label] = score
        self.event(
            "score",
            sandbox_id=vm.id,
            label=label,
            reward=score,
            expected=EXPECTED_RESULT,
            actual=json.loads(artifact),
        )
        (self.output / f"{label}-result.json").write_bytes(artifact)
        (self.output / f"{label}-files.zip").write_bytes(vm.files.download_dir(TASK_ROOT))
        require(score == 1, f"Task failed for {label}")
        return artifact

    def finalize(self):
        """Clean up sandboxes/snapshots and write the manifest. Always run from a `finally` block."""
        cleanup_errors = []
        for sandbox in reversed(self.sandboxes):
            try:
                sandbox.kill()
                self.event("deleted_sandbox", id=sandbox.id)
            except Exception as exc:
                cleanup_errors.append({"sandbox_id": sandbox.id, "error": str(exc)})
        for snapshot in reversed(self.snapshots):
            try:
                snapshot.delete()
                self.event("deleted_snapshot", id=snapshot.id)
            except Exception as exc:
                cleanup_errors.append({"snapshot_id": snapshot.id, "error": str(exc)})
        self.manifest["cleanup_errors"] = cleanup_errors

        self.manifest["sha256"] = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.output.iterdir()
            if p.is_file()
        }
        (self.output / "manifest.json").write_text(json.dumps(self.manifest, indent=2) + "\n")
        print(f"Evidence: {self.output.resolve()}; status: {self.manifest['status']}")
        if cleanup_errors:
            print("Cleanup incomplete; see resource IDs in manifest.json")


def demonstrate_lifecycle(run: Run) -> None:
    """Exercise reset, checkpoint/resume, and checkpoint-restore against one worker task."""
    source = run.create_sandbox("source", from_template="superserve/python-3.11")
    source.files.write(TASK_ROOT + "/worker.py", Path(__file__).with_name("worker.py").read_text())
    source.files.write(TASK_ROOT + "/marker.txt", "starting-state\n")
    run.run_command(source, START_WORKER)
    run.run_command(source, "python3 -c " + shlex.quote(WAIT_FOR_WORKER))

    initial = run.task_state(source)
    require(initial["index"] == 0 and initial["total"] == 0, "Invalid starting state")
    baseline = run.take_snapshot(source, "baseline")

    # Checkpoint and pause/resume: continue the same sandbox.
    trial = run.create_sandbox("trial", from_snapshot=baseline)
    require(run.task_state(trial) == initial, "Baseline fork lost process memory")
    run.step_task(trial)
    midpoint = run.step_task(trial)
    require(midpoint["index"] == 2 and midpoint["total"] == 34, "Invalid midpoint")
    trial.files.write(TASK_ROOT + "/marker.txt", "midpoint\n")
    checkpoint = run.take_snapshot(trial, "midpoint")

    run.lifecycle("pause", lambda: trial.pause(wait=True))
    require(trial.get_info().status.value == "paused", "Pause did not complete")
    run.lifecycle("resume", trial.resume)
    require(run.task_state(trial) == midpoint, "Pause/resume lost memory state")
    require(trial.files.read_text(TASK_ROOT + "/marker.txt") == "midpoint\n", "Resume lost disk state")
    expected_artifact = run.finish_task(trial, "resumed")

    # Restore an earlier checkpoint: a new sandbox created from the saved snapshot.
    restored = run.create_sandbox("checkpoint", from_snapshot=checkpoint)
    require(run.task_state(restored) == midpoint, "Checkpoint restore lost memory state")
    require(restored.files.read_text(TASK_ROOT + "/marker.txt") == "midpoint\n", "Checkpoint lost disk state")
    run.run_command(restored, f"test ! -e {TASK_ROOT}/result.json")
    require(run.finish_task(restored, "checkpoint") == expected_artifact, "Checkpoint result differs")

    # Reset: a fresh sandbox from the immutable baseline, not an in-place reset() API.
    reset = run.create_sandbox("reset", from_snapshot=baseline)
    require(run.task_state(reset) == initial, "Reset retained task progress")
    require(reset.files.read_text(TASK_ROOT + "/marker.txt") == "starting-state\n", "Reset retained file edits")
    run.run_command(reset, f"test ! -e {TASK_ROOT}/result.json")
    require(run.finish_task(reset, "reset") == expected_artifact, "Reset result differs")

    # Isolation: forking never mutated the original source sandbox.
    require(run.task_state(source) == initial, "Fork changed the source process")
    require(source.files.read_text(TASK_ROOT + "/marker.txt") == "starting-state\n", "Fork changed source files")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("runs"))
    args = parser.parse_args()
    if not os.environ.get("SUPERSERVE_API_KEY"):
        parser.error("Set SUPERSERVE_API_KEY before running this live example")

    run = Run.start(args.output)
    try:
        demonstrate_lifecycle(run)
        run.manifest["status"] = "passed"
    except BaseException as error:
        run.manifest["status"] = "failed"
        run.manifest["error"] = str(error)
        run.event("error", message=str(error))
        raise
    finally:
        run.finalize()


if __name__ == "__main__":
    main()
