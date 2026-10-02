"""Run against real Superserve VMs; export evidence even if a check fails."""

import argparse
import hashlib
import json
import os
import shlex
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from superserve import Sandbox, __version__

ROOT = "/tmp/lifecycle-task"
EXPECTED = {"count": 5, "sum": 171}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("runs"))
    args = parser.parse_args()
    if not os.environ.get("SUPERSERVE_API_KEY"):
        parser.error("Set SUPERSERVE_API_KEY before running this live example")
    run_id = uuid.uuid4().hex[:12]
    output = args.output / run_id
    output.mkdir(parents=True)
    sandboxes, snapshots = [], []
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "sdk_version": __version__,
        "status": "running",
        "sandboxes": [],
        "snapshots": [],
        "scores": {},
    }

    def event(kind, **data):
        with (output / "events.jsonl").open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "time": datetime.now(timezone.utc).isoformat(),
                        "kind": kind,
                        **data,
                    }
                )
                + "\n"
            )

    def lifecycle(operation, call):
        start = time.monotonic()
        event("lifecycle_start", operation=operation)
        result = call()
        event(
            "lifecycle_end",
            operation=operation,
            duration_seconds=time.monotonic() - start,
        )
        return result

    def create(label, **kwargs):
        vm = lifecycle(
            "create:" + label,
            lambda: Sandbox.create(
                name=f"repro-{run_id}-{label}",
                timeout_seconds=300,
                auto_delete_seconds=3600,
                **kwargs,
            ),
        )
        sandboxes.append(vm)
        manifest["sandboxes"].append({"label": label, "id": vm.id})
        return vm

    def capture(vm, label):
        snap = lifecycle(
            "snapshot:" + label,
            lambda: vm.snapshot(
                name=f"repro-{run_id}-{label}", idempotency_key=f"{run_id}-{label}"
            ),
        )
        snapshots.append(snap)
        manifest["snapshots"].append({"label": label, "id": snap.id})
        return snap

    def command(vm, text):
        event("action", sandbox_id=vm.id, command=text)
        result = vm.commands.run(text, timeout_seconds=30)
        event(
            "observation",
            sandbox_id=vm.id,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
        )
        require(result.exit_code == 0, f"Command failed in {vm.id}: {result.stderr}")
        return result.stdout

    def state(vm, step=False):
        code = (
            "import urllib.request; print(urllib.request.urlopen("
            "urllib.request.Request('http://127.0.0.1:8765/"
            + ("step', method='POST'" if step else "state', method='GET'")
            + "), timeout=5).read().decode())"
        )
        return json.loads(command(vm, "python3 -c " + shlex.quote(code)))

    def finish(vm, label):
        current = state(vm)
        for _ in range(5 - current["index"]):
            state(vm, step=True)
        artifact = vm.files.read(ROOT + "/result.json")
        score = int(json.loads(artifact) == EXPECTED)
        manifest["scores"][label] = score
        event(
            "score",
            sandbox_id=vm.id,
            label=label,
            reward=score,
            expected=EXPECTED,
            actual=json.loads(artifact),
        )
        (output / f"{label}-result.json").write_bytes(artifact)
        (output / f"{label}-files.zip").write_bytes(vm.files.download_dir(ROOT))
        require(score == 1, f"Task failed for {label}")
        return artifact

    try:
        source = create("source", from_template="superserve/python-3.11")
        source.files.write(
            ROOT + "/worker.py", Path(__file__).with_name("worker.py").read_text()
        )
        source.files.write(ROOT + "/marker.txt", "starting-state\n")
        command(
            source,
            f"nohup python3 {ROOT}/worker.py >{ROOT}/worker.log 2>&1 </dev/null &",
        )
        # Readiness is bounded and uses the actual service, not a fixed delay.
        ready = "import time, urllib.request\nfor _ in range(50):\n try:\n  urllib.request.urlopen('http://127.0.0.1:8765/state', timeout=1); break\n except OSError: time.sleep(.1)\nelse: raise RuntimeError('worker did not start')"
        command(source, "python3 -c " + shlex.quote(ready))
        initial = state(source)
        require(
            initial["index"] == 0 and initial["total"] == 0, "Invalid starting state"
        )
        baseline = capture(source, "baseline")
        trial = create("trial", from_snapshot=baseline)
        require(state(trial) == initial, "Baseline fork lost process memory")
        state(trial, step=True)
        midpoint = state(trial, step=True)
        require(midpoint["index"] == 2 and midpoint["total"] == 34, "Invalid midpoint")
        trial.files.write(ROOT + "/marker.txt", "midpoint\n")
        checkpoint = capture(trial, "midpoint")

        lifecycle("pause", lambda: trial.pause(wait=True))
        require(trial.get_info().status.value == "paused", "Pause did not complete")
        lifecycle("resume", trial.resume)
        require(state(trial) == midpoint, "Pause/resume lost memory state")
        require(
            trial.files.read_text(ROOT + "/marker.txt") == "midpoint\n",
            "Resume lost disk state",
        )
        expected_artifact = finish(trial, "resumed")

        restored = create("checkpoint", from_snapshot=checkpoint)
        require(state(restored) == midpoint, "Checkpoint restore lost memory state")
        require(
            restored.files.read_text(ROOT + "/marker.txt") == "midpoint\n",
            "Checkpoint lost disk state",
        )
        command(restored, f"test ! -e {ROOT}/result.json")
        require(
            finish(restored, "checkpoint") == expected_artifact,
            "Checkpoint result differs",
        )

        # Reset is a fresh VM from the immutable baseline, not an in-place API.
        reset = create("reset", from_snapshot=baseline)
        require(state(reset) == initial, "Reset retained task progress")
        require(
            reset.files.read_text(ROOT + "/marker.txt") == "starting-state\n",
            "Reset retained file edits",
        )
        command(reset, f"test ! -e {ROOT}/result.json")
        require(finish(reset, "reset") == expected_artifact, "Reset result differs")
        require(state(source) == initial, "Fork changed the source process")
        require(
            source.files.read_text(ROOT + "/marker.txt") == "starting-state\n",
            "Fork changed source files",
        )
        manifest["status"] = "passed"
    except BaseException as error:
        manifest["status"] = "failed"
        manifest["error"] = str(error)
        event("error", message=str(error))
        raise
    finally:
        cleanup_errors = []
        for resource in reversed(sandboxes):
            try:
                resource.kill()
                event("deleted_sandbox", id=resource.id)
            except Exception as error:
                cleanup_errors.append({"sandbox_id": resource.id, "error": str(error)})
        for resource in reversed(snapshots):
            try:
                resource.delete()
                event("deleted_snapshot", id=resource.id)
            except Exception as error:
                cleanup_errors.append({"snapshot_id": resource.id, "error": str(error)})
        manifest["cleanup_errors"] = cleanup_errors
        manifest["sha256"] = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in output.iterdir()
            if p.is_file()
        }
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"Evidence: {output.resolve()}; status: {manifest['status']}")
        if cleanup_errors:
            print("Cleanup incomplete; see resource IDs in manifest.json")


if __name__ == "__main__":
    main()
