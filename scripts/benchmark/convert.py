"""Convert the blind task view's cases into Sherlog incident documents, one case at a time.

    python -I scripts/benchmark/convert.py TASKS_DIR OUT_DIR [--cases ID ...]

TASKS_DIR is one dataset of the task view (prepare.py makes it): cases/<id>/task.json, metrics.csv and, attached
next to them, logs.parquet and traces.parquet. Only these are read, never the answers file. Each document is
checked with the same validator as POST /api/triage and written to OUT_DIR/<id>.json; OUT_DIR/stats.jsonl gets
one line of counts per case. Needs pandas and pyarrow (the benchmark's environment), not the app's.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adapter  # noqa: E402
import byo  # noqa: E402  (adapter puts the repository root on the path)
import sim  # noqa: E402

WINDOW_SECONDS = sim.WINDOW_TICKS * sim.TICK_SECONDS


def read_metrics(case: Path) -> pd.DataFrame:
    """The metrics as the benchmark's runner prepares them for every method: duplicate time column dropped,
    infinities as missing, missing readings carried forward, then zero."""
    df = pd.read_csv(case / "metrics.csv")
    df = df.drop(columns=[c for c in ("time.1",) if c in df]).replace([np.inf, -np.inf], np.nan)
    return df.ffill().fillna(0)


def read_edges(case: Path, inject_time: int, entry: str | None) -> set[tuple[str, str]]:
    """Traced (caller, callee) service pairs from the spans that started before the injection: the call graph as
    it was while the system was healthy, so the fault cannot shape it. A root span was called by the system's
    untraced entry point, when it has one (adapter.ENTRY)."""
    path = case / "traces.parquet"
    if not path.exists():
        return set()
    spans = pd.read_parquet(path, columns=["spanID", "parentSpanID", "serviceName"],
                            filters=[("startTimeMillis", "<", inject_time * 1000)])
    owner = spans.drop_duplicates("spanID").set_index("spanID")["serviceName"]
    callers = spans["parentSpanID"].map(owner)
    if entry:  # a root span (no parent id at all; a parent outside the data is not one)
        callers = callers.mask(spans["parentSpanID"].fillna("").eq(""), entry)
    pairs = pd.DataFrame({"caller": callers, "callee": spans["serviceName"]}).dropna().drop_duplicates()
    return set(zip(pairs["caller"], pairs["callee"]))


def read_logs(case: Path, start: int) -> list[tuple[float, str, str]]:
    path = case / "logs.parquet"
    if not path.exists():
        return []
    logs = pd.read_parquet(path, columns=["timestamp", "container_name", "message"],
                           filters=[("timestamp", ">=", start), ("timestamp", "<", start + WINDOW_SECONDS)])
    return list(zip(logs["timestamp"].astype(float), logs["container_name"].astype(str), logs["message"].astype(str)))


def convert(case: Path) -> tuple[dict, dict]:
    task = json.loads((case / "task.json").read_text())
    inject_time, dataset = int(task["inject_time"]), task["dataset"]
    start = adapter.window_start(inject_time)
    metrics = read_metrics(case)
    rows = metrics[(metrics["time"] >= start - sim.TICK_SECONDS) & (metrics["time"] < start + WINDOW_SECONDS)]
    columns = {c: rows[c].astype(float).tolist() for c in rows.columns if c != "time"}
    series = adapter.series_of(columns, rows["time"].astype(float).tolist(), start)
    services = tuple(series)
    edges = read_edges(case, inject_time, adapter.ENTRY.get(adapter.system(dataset)))
    calls = adapter.merge(adapter.calls_from_edges(edges, services), adapter.datastore_calls(services),
                          adapter.hand_calls(dataset, services))
    logs = adapter.log_lines(read_logs(case, start), services, start)
    doc = adapter.to_doc(series, calls, logs)
    inc = byo.to_incident(byo.IncidentDoc.model_validate(doc))  # the upload checks, unchanged
    last_row = int(metrics["time"].max())
    stats = {
        "case_id": task["case_id"], "dataset": dataset, "services": len(services),
        "candidates": len(task["candidate_services"]), "metrics": doc["metrics"],
        "series": sum(len(m) for m in series.values()), "traced_pairs": len(edges),
        "calls": sum(len(c) for c in doc["calls"].values()), "log_lines": len(logs),
        "ticks_without_data": max(0, (start + WINDOW_SECONDS - 1 - last_row) // sim.TICK_SECONDS),
        "alarm": inc.alarm, "now": inc.now,
    }
    return doc, stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tasks", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--cases", nargs="*", help="only these case ids")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    cases = sorted((args.tasks / "cases").iterdir())
    if args.cases:
        cases = [c for c in cases if c.name in set(args.cases)]
    with (args.out / "stats.jsonl").open("a") as log:
        for case in cases:
            target = args.out / f"{case.name}.json"
            if target.exists():
                continue
            doc, stats = convert(case)
            target.write_text(json.dumps(doc, separators=(",", ":")))
            log.write(json.dumps(stats) + "\n")
            log.flush()
            print(json.dumps(stats), flush=True)


if __name__ == "__main__":
    main()
