"""Re-run published baselines with the benchmark's own runner (main.py), on every case, and record their rankings
by anonymised case id.

    python -I scripts/benchmark/baselines.py MAIN_PY DATA_DIR TASKS_DIR WORK_DIR baro nsigma ...

main.py reads DATA_DIR/RE2/<suite> as it ships (WORK_DIR/<method>-<dataset>/data links to it) and writes one result
file per case, named after the case's directory. Those names are mapped to case ids through the exporter's answers
file; the ranking is kept as main.py wrote it, cut to its first 20 entries (scoring reads the first 5 services).
Its printed report goes to runs/real-benchmark/baselines/<method>-<dataset>.txt. Run in the benchmark's
environment.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "runs" / "real-benchmark" / "baselines"
DATASETS = ("re2-ob", "re2-ss", "re2-tt")
KEPT = 20


def run(main_py: Path, data: Path, tasks: Path, work: Path, method: str, dataset: str) -> list[dict]:
    cwd = work / f"{method}-{dataset}"
    (cwd / "output" / "results").mkdir(parents=True, exist_ok=True)
    for stale in (cwd / "output" / "results").glob("*.json"):
        stale.unlink()
    if not (cwd / "data").exists():
        (cwd / "data").symlink_to(data)
    started = time.monotonic()
    done = subprocess.run([sys.executable, str(main_py), "--method", method, "--dataset", dataset], cwd=cwd,
                          capture_output=True, text=True)
    wall = time.monotonic() - started
    report = done.stdout[done.stdout.find("--- Evaluation"):] if "--- Evaluation" in done.stdout else done.stdout[-2000:]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{method}-{dataset}.txt").write_text(f"exit {done.returncode}, {wall:.0f} s\n{report}\n{done.stderr[-1500:]}")
    if done.returncode:
        raise RuntimeError(f"{method} on {dataset} failed: {done.stderr[-500:]}")
    by_source = {a["source"]: a["case_id"] for a in map(json.loads, (tasks / f"{dataset}.answers.jsonl").open())}
    rows = []
    for path in sorted((cwd / "output" / "results").glob("*.json")):
        service, fault, case = path.stem.split("_")
        ranks = json.loads(path.read_text())["0"]
        rows.append({"case_id": by_source[f"{service}_{fault}/{case}"], "method": method, "ranks": ranks[:KEPT],
                     "wall_s_per_case": round(wall / len(by_source), 3)})
    assert len(rows) == len(by_source), f"{method} on {dataset}: {len(rows)} results for {len(by_source)} cases"
    return rows


def main() -> None:
    main_py, data, tasks, work = (Path(a) for a in sys.argv[1:5])
    for method in sys.argv[5:]:
        rows = [r for d in DATASETS for r in run(main_py, data, tasks, work, method, d)]
        (OUT / f"{method}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in sorted(rows, key=lambda r: r["case_id"])))
        print(f"{method}: {len(rows)} cases", flush=True)


if __name__ == "__main__":
    main()
