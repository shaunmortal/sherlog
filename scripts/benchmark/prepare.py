"""Make the blind task view the benchmark's own exporter makes, attach each case's logs and traces, and write the
labels the scorer needs.

    python -I scripts/benchmark/prepare.py RAW_DIR PARQUET_DIR TASKS_DIR [DATASET ...]
    python -I scripts/benchmark/prepare.py RAW_DIR PARQUET_DIR TASKS_DIR re3-ob re3-ss re3-tt   # the RE3 suite

RAW_DIR holds one suite in the benchmark's archive layout (RE2-OB, RE2-SS, RE2-TT, each <case dir>/<n>/
with simple_metrics.csv and inject_time.txt): what its runner reads. PARQUET_DIR holds the Parquet copy of the
same cases (<suite><system>_<case dir>_<n>/logs.parquet and traces.parquet). The exporter names every case with an
anonymised id and keeps the labels in TASKS_DIR/<dataset>.answers.jsonl, away from the cases. This step reads
those labels only to find each case's logs and traces, which it hardlinks into the case directory unchanged.

RE3's cases come from the Parquet copy alone, so they are first laid out in RAW_DIR: metrics.parquet, which holds
exactly the archive's simple_metrics.csv, back as that file, and the log and trace series the multi-source methods
read (logts.csv, tracets_err.csv, tracets_lat.csv), which RE3 does not ship, built from the case's logs and traces
by the benchmark's own derive step (the one `rcaeval-convert derive` runs).

It also writes the suite's answers.jsonl (runs/real-benchmark/ for RE2, runs/real-benchmark/re3/ for RE3): per
anonymised case id, the root-cause service and fault, for scoring (no telemetry). Run it in the benchmark's
environment (it imports the exporter, and drain3 for the derive step).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from RCAEval.agent.export import export

DATASETS = {"re2-ob": "RE2-OB", "re2-ss": "RE2-SS", "re2-tt": "RE2-TT",
            "re3-ob": "RE3-OB", "re3-ss": "RE3-SS", "re3-tt": "RE3-TT"}
ATTACHED = ("logs.parquet", "traces.parquet")
RUNS = Path(__file__).resolve().parents[2] / "runs" / "real-benchmark"
KEPT = ("case_id", "dataset", "service", "fault", "n_candidate_services", "answer_in_candidates")
NS_PER_S = 1_000_000_000  # the derive step bins log timestamps given in nanoseconds; the Parquet copy's are seconds


def attach(src: Path, dst: Path) -> None:
    if dst.exists():
        return
    try:
        os.link(src, dst)  # never a symlink: its target would name the case's answer-bearing directory
    except OSError:
        shutil.copyfile(src, dst)


def lay_out(parquet: Path, out: Path, dataset: str) -> None:
    """The archive layout the runner and the exporter read, from the Parquet copy, one case at a time."""
    import pandas as pd
    import pyarrow.parquet as pq
    from RCAEval.convert.derive import log_series, trace_series

    def as_csv(path: Path) -> pd.DataFrame:  # plain dtypes, a missing number as NaN: what derive gets from a CSV
        return pq.read_table(path).to_pandas(ignore_metadata=True)

    for hub in sorted(parquet.glob(f"{dataset.replace('-', '')}_*")):
        case_dir, repetition = hub.name.split("_", 1)[1].rsplit("_", 1)
        case = out / case_dir / repetition
        if (case / "simple_metrics.csv").exists():  # written last: the case is complete
            continue
        case.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(hub / "inject_time.txt", case / "inject_time.txt")
        inject_time = int((hub / "inject_time.txt").read_text().split()[0])
        if (hub / "traces.parquet").exists():
            err, lat = trace_series(as_csv(hub / "traces.parquet"), inject_time)
            err.to_csv(case / "tracets_err.csv", index=False)
            lat.to_csv(case / "tracets_lat.csv", index=False)
        logs = as_csv(hub / "logs.parquet")
        logts, cluster_info, _ = log_series(logs.assign(timestamp=logs["timestamp"] * NS_PER_S))
        logts.to_csv(case / "logts.csv", index=False)
        (case / "cluster_info.json").write_text(json.dumps(cluster_info, indent=4))
        pd.read_parquet(hub / "metrics.parquet").to_csv(case / "simple_metrics.csv.part", index=False)
        os.replace(case / "simple_metrics.csv.part", case / "simple_metrics.csv")
        print(f"{dataset}: {len(list(out.glob('*/*/simple_metrics.csv')))} cases laid out", flush=True)


def write_answers(tasks: Path, suite: str) -> None:
    rows = []
    for dataset in (d for d in DATASETS if d.startswith(suite)):  # every system of the suite exported so far
        path = tasks / f"{dataset}.answers.jsonl"
        for answer in (json.loads(line) for line in (path.read_text().splitlines() if path.exists() else [])):
            rows.append({**{k: answer[k] for k in KEPT}, "repetition": int(answer["source"].rsplit("/", 1)[1])})
    answers = (RUNS if suite == "re2" else RUNS / suite) / "answers.jsonl"  # as report.SUITES
    answers.parent.mkdir(parents=True, exist_ok=True)
    answers.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"{len(rows)} cases; labels in {answers}")


def main() -> None:
    raw, parquet, tasks = (Path(a) for a in sys.argv[1:4])
    datasets = sys.argv[4:] or [d for d in DATASETS if d.startswith("re2")]
    for dataset in datasets:
        task_dir = tasks / dataset
        answers_path = tasks / f"{dataset}.answers.jsonl"
        if not answers_path.exists():
            if dataset.startswith("re3"):
                lay_out(parquet, raw / DATASETS[dataset], dataset)
            export(dataset, out_dir=str(tasks), data_root=str(raw / DATASETS[dataset]), download=False)
        for line in answers_path.read_text().splitlines():
            answer = json.loads(line)
            hub = parquet / f"{dataset.replace('-', '')}_{answer['source'].replace('/', '_')}"
            for name in ATTACHED:
                if (hub / name).exists():
                    attach(hub / name, task_dir / "cases" / answer["case_id"] / name)
    for suite in sorted({d[:3] for d in datasets}):
        write_answers(tasks, suite)


if __name__ == "__main__":
    main()
