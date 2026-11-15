"""Re-run published baselines with the benchmark's own runner (main.py), on every case, and record their rankings
by anonymised case id.

    python -I scripts/benchmark/baselines.py MAIN_PY DATA_DIR TASKS_DIR WORK_DIR baro nsigma circa:re2-ob,re2-ss
    python -I scripts/benchmark/baselines.py MAIN_PY DATA_DIR TASKS_DIR WORK_DIR nsigma/10 nsigma/2   # other windows
    python -I scripts/benchmark/baselines.py MAIN_PY DATA_DIR TASKS_DIR WORK_DIR --suite re3 baro nsigma

main.py reads DATA_DIR/RE2/<system> (DATA_DIR/RE3/<system> for RE3) as it ships (WORK_DIR/<method>-<dataset>/data links to it) and writes one result
file per case, named after the case's directory. Those names are mapped to case ids through the exporter's answers
file; the ranking is kept as main.py wrote it, cut to its first 20 entries (scoring reads the first 5 services).
main.py stops at a method's first exception; such a case is left out, the method re-run on the rest, and the case
recorded as failed with an empty ranking (a miss at every k).
Its printed report goes to runs/real-benchmark/baselines/<method>-<dataset>.txt (re3/baselines/ for RE3). `method/LENGTH` passes the
runner's --length (minutes of data around the injection, half on each side; its default is 20) and records the
method as <method>-len<LENGTH>. Run in the benchmark's environment.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

RUNS = Path(__file__).resolve().parents[2] / "runs" / "real-benchmark"
DATASETS = {"re2-ob": "RE2/RE2-OB", "re2-ss": "RE2/RE2-SS", "re2-tt": "RE2/RE2-TT",
            "re3-ob": "RE3/RE3-OB", "re3-ss": "RE3/RE3-SS", "re3-tt": "RE3/RE3-TT"}
KEPT = 20
MAX_CRASHES = 5


def out_dir(dataset: str) -> Path:
    suite = dataset[:3]
    return (RUNS if suite == "re2" else RUNS / suite) / "baselines"  # as report.SUITES


def _cases(root: Path) -> list[Path]:
    """The case directories in the order main.py runs them (it sorts the metrics files' paths)."""
    return [Path(p).parent for p in sorted(map(str, root.glob("**/simple_metrics.csv")))]


def _data_without(data: Path, folder: str, excluded: set[str], into: Path) -> Path:
    """A copy of the data tree made of links, minus some cases: main.py stops at a method's first exception, so a
    case a method crashes on is left out and the method runs on the rest."""
    if into.exists():
        shutil.rmtree(into)
    for case in _cases(data / folder):
        rel = case.relative_to(data / folder)
        if rel.as_posix() not in excluded:
            (into / folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (into / folder / rel).symlink_to(case)
    return into


def run(main_py: Path, data: Path, tasks: Path, work: Path, method: str, dataset: str, length: str = "") -> list[dict]:
    name = f"{method}-len{length}" if length else method
    cwd, folder = work / f"{name}-{dataset}", DATASETS[dataset]
    results = cwd / "output" / "results"
    excluded: dict[str, str] = {}  # case directory -> the error it stopped main.py with
    started = time.monotonic()
    for _ in range(MAX_CRASHES + 1):
        results.mkdir(parents=True, exist_ok=True)
        for stale in results.glob("*.json"):
            stale.unlink()
        link = cwd / "data"
        if link.is_symlink() or link.exists():
            link.unlink() if link.is_symlink() else shutil.rmtree(link)
        link.symlink_to(_data_without(data, folder, set(excluded), work / f"{name}-{dataset}-data")
                        if excluded else data)
        window = ["--length", length] if length else []
        done = subprocess.run([sys.executable, str(main_py), "--method", method, "--dataset", dataset, *window],
                              cwd=cwd, capture_output=True, text=True)
        if done.returncode == 0:
            break
        finished = {p.stem for p in results.glob("*.json")}
        order = [c.relative_to(data / folder).as_posix() for c in _cases(data / folder)]
        crashed = next(c for c in order if c not in excluded and c.replace("/", "_") not in finished)
        excluded[crashed] = done.stderr.strip().splitlines()[-1][:300]
    else:
        raise RuntimeError(f"{method} on {dataset} crashed on more than {MAX_CRASHES} cases")
    wall = time.monotonic() - started
    start = done.stdout.find("--- Evaluation")
    report = done.stdout[start:] if start >= 0 else done.stdout[-2000:]
    out_dir(dataset).mkdir(parents=True, exist_ok=True)
    crashes = "".join(f"left out (main.py stopped on it): a case, {err}\n" for err in excluded.values())
    (out_dir(dataset) / f"{name}-{dataset}.txt").write_text(f"{wall:.0f} s, {len(excluded)} case(s) left out\n{crashes}{report}")
    by_source = {a["source"]: a["case_id"] for a in map(json.loads, (tasks / f"{dataset}.answers.jsonl").open())}
    rows = []
    for source, case_id in by_source.items():
        path = results / f"{source.replace('/', '_')}.json"
        failed = source in excluded or not path.exists()
        ranks = [] if failed else json.loads(path.read_text())["0"]
        rows.append({"case_id": case_id, "method": name, "ranks": ranks[:KEPT], "failed": failed,
                     "wall_s_per_case": round(wall / len(by_source), 3)})
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("main_py", "data", "tasks", "work"):
        parser.add_argument(name, type=Path)
    parser.add_argument("specs", nargs="+", help="method[/LENGTH][:dataset,dataset] (some systems only, for a method "
                                                 "too slow on one)")
    parser.add_argument("--suite", default="re2", choices=("re2", "re3"))
    args = parser.parse_args()
    for spec in args.specs:
        head, _, only = spec.partition(":")
        method, _, length = head.partition("/")
        datasets = only.split(",") if only else [d for d in DATASETS if d.startswith(args.suite)]
        rows = [r for d in datasets for r in run(args.main_py, args.data, args.tasks, args.work, method, d, length)]
        rows = sorted(rows, key=lambda r: r["case_id"])
        name = rows[0]["method"]
        (out_dir(datasets[0]) / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        print(f"{name}: {len(rows)} cases", flush=True)


if __name__ == "__main__":
    main()
