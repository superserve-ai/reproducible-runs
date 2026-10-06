# Reproducible runs on Superserve

A runnable infrastructure example: reset an environment, checkpoint and resume
its processes, restore an earlier checkpoint, and export a run for inspection.

The workload is deliberately deterministic: a Python service accumulates five
numbers. Its progress and a random process identity token live only in memory.
Restarting the service cannot recover that state. Files provide a separate check
of disk persistence. No model API key, external dataset, or agent framework is
required.

## Run

Requires Python 3.9+, a Superserve API key, and saved snapshots enabled on the
deployment you use. This creates four small VMs and two saved snapshots; normal
Superserve usage charges apply. Resources are deleted in `finally`, and VMs also
have a five-minute auto-pause and deletion after one continuous hour paused. If cleanup
fails, the manifest lists resource IDs to delete manually. Saved snapshots have
no automatic cleanup deadline in this example.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export SUPERSERVE_API_KEY="your-key"
python run.py
```

Use `python run.py --output /path/to/results` to change the export directory.
Do not commit credentials or generated run output.

## Follow the intermediate states

The script prints lifecycle operations as they happen, then prints the observed
memory and disk state at each verification point. For example (IDs shortened):

```text
[PASS] before checkpoint: step=2 sum=34 memory=abc12345 marker=midpoint result=absent vm=trial-id
[PASS] after resume (same VM): step=2 sum=34 memory=abc12345 marker=midpoint result=absent vm=trial-id
[PASS] resumed complete: step=5 sum=171 memory=abc12345 marker=midpoint result=present vm=trial-id
[PASS] checkpoint restored (new VM): step=2 sum=34 memory=abc12345 marker=midpoint result=absent vm=checkpoint-id
[PASS] reset baseline (new VM): step=0 sum=0 memory=abc12345 marker=starting-state result=absent vm=reset-id
```

Read these rows in order: after the trial finishes at step 5, checkpoint restore
returns to step 2 and removes the later result file. Reset returns to step 0 and
the original disk marker. Pause/resume keeps the trial's VM ID; restore and reset
have different VM IDs. The matching memory token shows that the process state
was restored, rather than the worker being restarted.

All three final result files intentionally contain the same count and sum.
They demonstrate reproducibility after completing the task; the differences
between the lifecycle operations are in the intermediate states. Open
`summary.md` in the export directory for a comparison table, or `states.json`
for full observed and expected values. Failed comparisons are saved and marked
`FAIL` before the script exits nonzero.

## What the example proves

| Operation | Mechanism | Passing check |
| --- | --- | --- |
| Starting state | Start service and capture a saved snapshot | Fork retains its random memory token and zero progress |
| Checkpoint | Capture after two steps | Saved memory has index 2 and sum 34; disk marker is `midpoint` |
| Pause/resume | Pause the trial VM, wait for `paused`, then resume | Memory token, progress, and disk marker are unchanged |
| Restore checkpoint | Create another VM from the midpoint snapshot after the original finishes | Progress returns to the midpoint and the later result file is absent |
| Reset | Create a fresh VM from the baseline snapshot | Progress returns to zero, disk edits disappear, and no result file remains |
| Reproducibility | Complete all three trials | All produce the same result bytes: count 5, sum 171 |
| Isolation | Inspect the source after running forks | Source memory and marker are unchanged |

Reset here means replacing the environment with a new sandbox ID from an immutable
baseline. It does not call an in-place `reset()` API. Pause/resume continues the
same sandbox; restoring a saved checkpoint creates a new one.

The task advances only on a request, so snapshot verification has no timing race
with background progress. This demonstrates memory and disk restoration, not
preservation of external TCP connections or remote service state. The live run
is intended to be short; it is not a duration or concurrency benchmark.

## Export format

Each invocation writes `runs/<run-id>/`:

- `events.jsonl`: controller-recorded commands, stdout/stderr, exit codes,
  lifecycle operations with measured durations, scores, and cleanup events.
- `manifest.json`: overall status, SDK version, sandbox and snapshot IDs,
  per-trial rewards, file SHA-256 hashes, and cleanup failures if any.
- `summary.md`: a readable table of observed intermediate and completed states.
- `states.json`: each state's stage, VM ID, full memory token, progress, disk
  marker, result-file presence, expected values, and passing/failing comparison.
- `{resumed,checkpoint,reset}-result.json`: final task outputs.
- `{resumed,checkpoint,reset}-files.zip`: exported task directories, including
  the worker source, marker, log, and result.

A successful run reports `status: passed`, with reward 1 for each trial. Failed
checks exit nonzero and preserve partial evidence. No passing live output is
bundled in this repository; run it against your deployment to generate evidence.

The reward is computed by the host controller against an expected result; it is
not a score supplied by the task. This small fixture checks lifecycle behavior.
It is not a hardened grader for an adversarial agent with unrestricted shell
access, nor a claim of agent performance.

## Plug in your own agent or evaluator

Replace calls to `/step` with your harness's tool actions, recording each action
and observation through the controller. Keep grading outside the sandbox and
export the grader's score alongside the artifacts. The example's trajectory is
recorded by this harness, not automatically reconstructed by the platform.
It does not export arbitrary network traffic or model reasoning.

Snapshot restoration reproduces VM state. Reproducing a whole agent rollout also
requires controlling model sampling, external APIs, clocks, and random inputs.
Snapshot IDs in the manifest are lineage references: this example deletes the
snapshots after exporting results, so those IDs cannot be restored afterward.

## Documentation

- [Snapshots and forks](https://docs.superserve.ai/sandbox/snapshots)
- [Pause and resume](https://docs.superserve.ai/sandbox/lifecycle)
- [File export](https://docs.superserve.ai/filesystem/read-write)
- [API keys](https://docs.superserve.ai/api-key)

## Local checks

```bash
python -m unittest discover -s . -p 'test_*.py' -v
python -m compileall -q run.py worker.py
```

Local tests exercise the real worker over HTTP, confirm a restart loses its
memory-only progress, and check state reporting and failure evidence. Only a
credentialed `python run.py` can verify the VM lifecycle operations.

## Run in GitHub Actions

Add `SUPERSERVE_API_KEY` under **Settings → Secrets and variables → Actions →
New repository secret**. Then open **Actions → Live lifecycle demo → Run workflow**.
The manual workflow runs the real VM checks and uploads the resulting evidence
as a workflow artifact, including partial evidence on failure. Anyone who can
download repository workflow artifacts can inspect those logs and files.

The local-checks workflow uses no credentials. Live runs happen only when
manually requested, never on a pull request.
