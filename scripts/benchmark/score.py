"""Score every method's test-set rankings with the benchmark's own evaluator (its `evaluate` command).

    python -I scripts/benchmark/score.py WORK_DIR [SUITE]      # re2 (default) or re3

Writes, under WORK_DIR, the test cases' answers per system and the predictions file the evaluator reads for each
method, runs the evaluator on them, and keeps its service-level AC@1, AC@3, AC@5 and Avg@5 in
runs/real-benchmark/official.json (re3/official.json for RE3). report.py then checks its own numbers against these. A live method that did
not reach every test case is scored on the cases it ran (the evaluator would count the rest as misses), and
report.py says how many those are. Run in the benchmark's environment.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report  # noqa: E402

EVALUATOR = [sys.executable, "-m", "RCAEval.agent"]  # the evaluator's command-line entry point


def evaluate(work: Path, name: str, answers: list[dict], recs: dict[str, dict]) -> dict:
    run = [a for a in answers if a["case_id"] in recs]
    answers_path, preds_path, out = (work / f"{name}.{x}" for x in ("answers.jsonl", "preds.jsonl", "summary.json"))
    answers_path.write_text("".join(json.dumps(a) + "\n" for a in run))
    preds_path.write_text("".join(json.dumps({"case_id": a["case_id"], "ranks": recs[a["case_id"]]["ranks"]}) + "\n"
                                  for a in run))
    subprocess.run([*EVALUATOR, "evaluate", "--answers", str(answers_path), "--predictions", str(preds_path),
                    "--json-out", str(out)], check=True, capture_output=True)
    service = json.loads(out.read_text())["service"]
    return {"n": service["n"], **{k: service[k] for k in ("AC@1", "AC@3", "AC@5", "Avg@5")}}


def main() -> None:
    suite = sys.argv[2] if len(sys.argv) > 2 else "re2"
    runs, systems = report.SUITES[suite], report.systems(suite)
    work = Path(sys.argv[1]) / ("scoring" if suite == "re2" else f"scoring-{suite}")
    work.mkdir(parents=True, exist_ok=True)
    tasks = Path(sys.argv[1]) / "tasks"
    test = set(json.loads((runs / "split.json").read_text())["test"])
    full = [json.loads(line) for d in systems for line in (tasks / f"{d}.answers.jsonl").open()]
    answers = [a for a in full if a["case_id"] in test]
    official = {}
    for method in report.LABELS:
        recs = report.records(method, test, "test", runs)
        if not recs:
            continue
        groups = {"all": answers, **{f"system {s}": [a for a in answers if a["dataset"] == d]
                                     for d, s in systems.items()}}
        official[method] = {g: evaluate(work, f"{method}-{g.replace(' ', '-')}", rows, recs)
                            for g, rows in groups.items() if any(a["case_id"] in recs for a in rows)}
        print(method, json.dumps(official[method]["all"]), flush=True)
    (runs / "official.json").write_text(json.dumps(official, indent=1) + "\n")


if __name__ == "__main__":
    main()
