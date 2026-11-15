"""Make the blind task view the benchmark's own exporter makes, attach each case's logs and traces, and write the
labels the scorer needs.

    python -I scripts/benchmark/prepare.py RAW_DIR PARQUET_DIR TASKS_DIR [DATASET ...]

RAW_DIR holds the three suites in the benchmark's archive layout (RE2-OB, RE2-SS, RE2-TT, each <case dir>/<n>/
with simple_metrics.csv and inject_time.txt): what its runner reads. PARQUET_DIR holds the Parquet copy of the
same cases (<suite><system>_<case dir>_<n>/logs.parquet and traces.parquet). The exporter names every case with an
anonymised id and keeps the labels in TASKS_DIR/<dataset>.answers.jsonl, away from the cases. This step reads
those labels only to find each case's logs and traces, which it hardlinks into the case directory unchanged.

It also writes runs/real-benchmark/answers.jsonl: per anonymised case id, the root-cause service and fault, for
scoring (no telemetry). Run it in the benchmark's environment (it imports the exporter).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from RCAEval.agent.export import export

DATASETS = {"re2-ob": "RE2-OB", "re2-ss": "RE2-SS", "re2-tt": "RE2-TT"}
ATTACHED = ("logs.parquet", "traces.parquet")
ANSWERS = Path(__file__).resolve().parents[2] / "runs" / "real-benchmark" / "answers.jsonl"
KEPT = ("case_id", "dataset", "service", "fault", "n_candidate_services", "answer_in_candidates")


def attach(src: Path, dst: Path) -> None:
    if dst.exists():
        return
    try:
        os.link(src, dst)  # never a symlink: its target would name the case's answer-bearing directory
    except OSError:
        shutil.copyfile(src, dst)


def main() -> None:
    raw, parquet, tasks = (Path(a) for a in sys.argv[1:4])
    for dataset in sys.argv[4:] or DATASETS:
        task_dir = tasks / dataset
        answers_path = tasks / f"{dataset}.answers.jsonl"
        if not answers_path.exists():
            export(dataset, out_dir=str(tasks), data_root=str(raw / DATASETS[dataset]), download=False)
        for line in answers_path.read_text().splitlines():
            answer = json.loads(line)
            hub = parquet / f"{dataset.replace('-', '')}_{answer['source'].replace('/', '_')}"
            for name in ATTACHED:
                if (hub / name).exists():
                    attach(hub / name, task_dir / "cases" / answer["case_id"] / name)
    rows = []
    for dataset in DATASETS:  # every suite exported so far
        path = tasks / f"{dataset}.answers.jsonl"
        for answer in (json.loads(line) for line in (path.read_text().splitlines() if path.exists() else [])):
            rows.append({**{k: answer[k] for k in KEPT}, "repetition": int(answer["source"].rsplit("/", 1)[1])})
    ANSWERS.parent.mkdir(parents=True, exist_ok=True)
    ANSWERS.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"{len(rows)} cases; labels in {ANSWERS}")


if __name__ == "__main__":
    main()
